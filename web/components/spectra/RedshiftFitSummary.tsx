'use client';

import React, { useMemo } from 'react';
import { Card } from '@/components/ui/Card';
import { Loader2, AlertCircle, Info } from 'lucide-react';
import type { Spectrum } from '@/lib/types';
import { useRedshiftFits } from '@/lib/hooks/useSpectrumJson';

interface RedshiftFitSummaryProps {
  spectra: (Spectrum & { observation?: string })[];
  redshift_auto: number | null;
}

interface GratingFit {
  grating: string;
  observation?: string;
  fitsPath: string;
  redshift?: number;
  chi2Min?: number;
  confidence?: number;
  isUsedForAuto: boolean;
  loading: boolean;
  error: string | null;
}

export const RedshiftFitSummary: React.FC<RedshiftFitSummaryProps> = ({
  spectra,
  redshift_auto,
}) => {
  // Shared with SpectrumPlot / the inspection prefetch via the TanStack cache
  // (lib/hooks/useSpectrumJson.ts), so the zfit JSON is fetched once per
  // spectrum per page instead of once per consumer (#500).
  const fitsPaths = useMemo(() => spectra.map(s => s.fits_path), [spectra]);
  const fitQueries = useRedshiftFits(fitsPaths);

  const gratingFits: GratingFit[] = useMemo(
    () => spectra.map((s, i) => {
      const q = fitQueries[i];
      const fitData = q?.data ?? null;
      const noFit = q?.isSuccess && fitData === null;
      // Null scalars (degenerate fit with no finite chi2 minimum) map to
      // undefined so the table's existing guards render an em dash.
      const redshift = fitData?.redshift ?? undefined;
      return {
        grating: s.grating,
        observation: s.observation,
        fitsPath: s.fits_path,
        redshift,
        chi2Min: fitData?.chi2_min ?? undefined,
        confidence: fitData?.confidence ?? undefined,
        loading: q?.isPending ?? true,
        error: noFit
          ? 'No fit available'
          : q?.isError
            ? (q.error instanceof Error ? q.error.message : 'Failed to load')
            : null,
        // Mark which redshift was used for auto (if it matches)
        isUsedForAuto:
          redshift_auto !== null &&
          redshift !== undefined &&
          Math.abs(redshift - redshift_auto) < 0.0001,
      };
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spectra, redshift_auto, ...fitQueries.map(q => q.data), ...fitQueries.map(q => q.status)],
  );

  const hasAnyFits = gratingFits.some(f => f.redshift !== undefined);
  const isLoading = gratingFits.some(f => f.loading);

  if (isLoading) {
    return (
      <Card className="p-6">
        <div className="flex items-center justify-center py-8">
          <Loader2 className="w-5 h-5 animate-spin text-primary mr-3" />
          <span className="text-text-secondary">Loading redshift fits...</span>
        </div>
      </Card>
    );
  }

  if (!hasAnyFits) {
    return (
      <Card className="p-6">
        <div className="flex flex-col items-center justify-center py-8 text-center">
          <AlertCircle className="w-6 h-6 text-text-secondary mb-2" />
          <p className="text-text-secondary">
            No redshift fitting data available for this object
          </p>
          <p className="text-xs text-text-secondary dark:text-text-tertiary mt-1">
            Redshift fits have not been computed for any grating
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 mb-4">
        <h3 className="text-lg font-semibold text-text-primary">Redshift Fit Summary</h3>
        <div className="group relative">
          <Info className="w-4 h-4 text-text-secondary cursor-help" />
          <div className="absolute left-0 top-6 w-64 p-2 bg-card border border-border text-text-primary text-xs rounded shadow-lg opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-10">
            If multiple gratings are available, the automatic redshift is determined from a decision tree, generally preferring PRISM redshifts but using grating redshifts if they agree.
          </div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-table-header">
            <tr className="border-b border-border">
              <th className="text-left py-2 px-3 text-sm font-medium text-text-secondary">
                Observation
              </th>
              <th className="text-left py-2 px-3 text-sm font-medium text-text-secondary">
                Grating
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary">
                Redshift
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary">
                χ²_min
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary">
                Confidence
              </th>
              <th className="text-center py-2 px-3 text-sm font-medium text-text-secondary">
                Used
              </th>
            </tr>
          </thead>
          <tbody>
            {gratingFits.map((fit, index) => (
              <tr
                key={index}
                className={`border-b border-border last:border-0 ${
                  fit.isUsedForAuto ? 'bg-green-50 dark:bg-green-950' : ''
                }`}
              >
                <td className="py-2 px-3 text-sm text-text-primary">
                  {fit.observation ?? <span className="text-text-secondary">—</span>}
                </td>
                <td className="py-2 px-3 text-sm font-medium text-text-primary">
                  {fit.grating}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary tabular-nums">
                  {fit.error ? (
                    <span className="text-text-secondary text-xs">{fit.error}</span>
                  ) : fit.redshift !== undefined ? (
                    fit.redshift.toFixed(4)
                  ) : (
                    <span className="text-text-secondary">—</span>
                  )}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary tabular-nums">
                  {fit.chi2Min !== undefined ? (
                    fit.chi2Min.toFixed(2)
                  ) : (
                    <span className="text-text-secondary">—</span>
                  )}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary tabular-nums">
                  {fit.confidence !== undefined ? (
                    `${fit.confidence.toFixed(1)}%`
                  ) : (
                    <span className="text-text-secondary">—</span>
                  )}
                </td>
                <td className="py-2 px-3 text-center">
                  {fit.isUsedForAuto && (
                    <span className="text-green-600 dark:text-green-400 font-bold" title="Used for redshift_auto">
                      ✓
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
};
