'use client';

import React, { useState, useCallback, useRef } from 'react';
import { ChevronDown, ChevronRight, AlertCircle, Loader2 } from 'lucide-react';
import { FilterChip, type FilterOption } from '@/components/ui/FilterChip';
import { SpectrumPlot } from './SpectrumPlot';
import { GratingDetails } from './GratingDetails';
import { DQ_FLAGS, decodeBitmask, encodeBitmask } from '@/lib/flags';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { Spectrum } from '@/lib/types';

const DQ_OPTIONS: FilterOption[] = DQ_FLAGS.map(f => ({
  value: f.value,
  label: f.label,
  icon: f.icon,
  color: f.color,
}));

function formatExposureTime(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds > 3600) return `${(seconds / 3600).toFixed(2)}h`;
  if (seconds > 60) return `${(seconds / 60).toFixed(1)}m`;
  return `${seconds.toFixed(0)}s`;
}

interface SpectrumDetailCardProps {
  spectrum: Spectrum;
  targetId: string;
  observation: string;
  programName: string;
  expanded: boolean;
  onToggle: () => void;
  /** True when this spectrum's redshift_auto matches objects.redshift_auto. */
  isSelected: boolean;
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
  observation,
  programName,
  expanded,
  onToggle,
  isSelected,
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

  const dqSelected = decodeBitmask(dqBitmask, DQ_FLAGS);

  return (
    <div
      id={cardId}
      className={`border rounded-lg overflow-hidden bg-card dark:bg-slate-800 ${
        isSelected
          ? 'border-emerald-400 dark:border-emerald-600 shadow-sm'
          : 'border-border dark:border-slate-700'
      }`}
    >
      {/* Header row — always visible */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
        <button
          onClick={onToggle}
          className="flex items-center gap-2 text-text-primary dark:text-slate-100 hover:opacity-80"
          aria-expanded={expanded}
        >
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
        </button>

        <span className="text-sm font-mono text-text-primary dark:text-slate-200">
          {targetId}
        </span>

        <span className="text-xs text-text-secondary dark:text-slate-400">
          {programName} · {observation}
        </span>

        <div className="flex items-center gap-3 text-xs text-text-secondary dark:text-slate-400 ml-auto">
          <span>
            <span className="opacity-70">z</span>{' '}
            <span className="font-mono text-text-primary dark:text-slate-200">
              {spectrum.redshift_auto != null ? spectrum.redshift_auto.toFixed(4) : '—'}
            </span>
          </span>
          <span>
            <span className="opacity-70">S/N</span>{' '}
            <span className="font-mono text-text-primary dark:text-slate-200">
              {spectrum.signal_to_noise != null ? spectrum.signal_to_noise.toFixed(1) : '—'}
            </span>
          </span>
          <span>
            <span className="opacity-70">t</span>{' '}
            <span className="font-mono text-text-primary dark:text-slate-200">
              {formatExposureTime(spectrum.exposure_time)}
            </span>
          </span>
          {isSelected && (
            <span className="text-emerald-600 dark:text-emerald-400 font-medium" title="This spectrum's redshift_auto sets the object's auto redshift">
              ← selected
            </span>
          )}
          <FilterChip
            label="DQ"
            options={DQ_OPTIONS}
            selected={dqSelected}
            onChange={handleDqChange}
            disabled={!canEdit || dqSaving}
            dropdownPlacement="bottom"
          />
          {dqSaving && <Loader2 className="w-3.5 h-3.5 animate-spin text-text-secondary dark:text-slate-400" />}
        </div>
      </div>

      {dqError && (
        <div className="px-4 py-1.5 bg-red-50 dark:bg-red-950/40 border-t border-red-200 dark:border-red-900 flex items-center gap-2 text-xs text-red-800 dark:text-red-300">
          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
          {dqError}
        </div>
      )}

      {/* Expandable body */}
      {expanded && (
        <div className="border-t border-border dark:border-slate-700 px-4 py-4 bg-background dark:bg-slate-900">
          <SpectrumPlot
            fitsPath={spectrum.fits_path}
            grating={spectrum.grating}
            initialRedshift={objectRedshift}
          />
          <div className="mt-4">
            <GratingDetails spectrum={spectrum} />
          </div>
        </div>
      )}
    </div>
  );
};
