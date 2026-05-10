'use client';

import React, { useState, useEffect } from 'react';
import { Card } from '@/components/ui/Card';
import { Loader2, AlertCircle, Info } from 'lucide-react';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';
import type { Spectrum } from '@/lib/types';

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
  const [gratingFits, setGratingFits] = useState<GratingFit[]>([]);

  useEffect(() => {
    // Initialize loading state for all gratings
    const initialFits: GratingFit[] = spectra.map(s => ({
      grating: s.grating,
      observation: s.observation,
      fitsPath: s.fits_path,
      isUsedForAuto: false, // Will be determined after loading
      loading: true,
      error: null,
    }));
    setGratingFits(initialFits);

    // Fetch zfit data for each spectrum
    const fetchAllFits = async () => {
      const promises = spectra.map(async (spectrum, index) => {
        try {
          const response = await fetch(
            `/api/redshift-fit?path=${encodeURIComponent(spectrum.fits_path)}`
          );

          if (!response.ok) {
            if (response.status === 404) {
              return {
                index,
                error: 'No fit available',
              };
            }
            throw new Error('Failed to load fit data');
          }

          const fitData: RedshiftFitData = await response.json();

          return {
            index,
            redshift: fitData.redshift,
            chi2Min: fitData.chi2_min,
            confidence: fitData.confidence,
          };
        } catch (err) {
          return {
            index,
            error: err instanceof Error ? err.message : 'Failed to load',
          };
        }
      });

      const results = await Promise.all(promises);

      setGratingFits(prev =>
        prev.map((fit, i) => {
          const result = results[i];
          return {
            ...fit,
            ...result,
            loading: false,
            // Mark which redshift was used for auto (if it matches)
            isUsedForAuto:
              redshift_auto !== null &&
              result.redshift !== undefined &&
              Math.abs(result.redshift - redshift_auto) < 0.0001,
          };
        })
      );
    };

    fetchAllFits();
  }, [spectra, redshift_auto]);

  const hasAnyFits = gratingFits.some(f => f.redshift !== undefined);
  const isLoading = gratingFits.some(f => f.loading);

  if (isLoading) {
    return (
      <Card className="p-6">
        <div className="flex items-center justify-center py-8">
          <Loader2 className="w-5 h-5 animate-spin text-primary mr-3" />
          <span className="text-text-secondary dark:text-slate-400">Loading redshift fits...</span>
        </div>
      </Card>
    );
  }

  if (!hasAnyFits) {
    return (
      <Card className="p-6">
        <div className="flex flex-col items-center justify-center py-8 text-center">
          <AlertCircle className="w-6 h-6 text-text-secondary dark:text-slate-400 mb-2" />
          <p className="text-text-secondary dark:text-slate-400">
            No redshift fitting data available for this object
          </p>
          <p className="text-xs text-text-secondary dark:text-slate-500 mt-1">
            Redshift fits have not been computed for any grating
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 mb-4">
        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">Redshift Fit Summary</h3>
        <div className="group relative">
          <Info className="w-4 h-4 text-text-secondary dark:text-slate-400 cursor-help" />
          <div className="absolute left-0 top-6 w-64 p-2 bg-gray-900 text-white text-xs rounded shadow-lg opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-10">
            If multiple gratings are available, the automatic redshift is determined from a decision tree, generally preferring PRISM redshifts but using grating redshifts if they agree.
          </div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border dark:border-slate-700">
              <th className="text-left py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                Observation
              </th>
              <th className="text-left py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                Grating
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                Redshift
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                χ²_min
              </th>
              <th className="text-right py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                Confidence
              </th>
              <th className="text-center py-2 px-3 text-sm font-medium text-text-secondary dark:text-slate-400">
                Used
              </th>
            </tr>
          </thead>
          <tbody>
            {gratingFits.map((fit, index) => (
              <tr
                key={index}
                className={`border-b border-border dark:border-slate-700 last:border-0 ${
                  fit.isUsedForAuto ? 'bg-green-50 dark:bg-green-950' : ''
                }`}
              >
                <td className="py-2 px-3 text-sm text-text-primary dark:text-slate-100">
                  {fit.observation ?? <span className="text-text-secondary dark:text-slate-400">—</span>}
                </td>
                <td className="py-2 px-3 text-sm font-medium text-text-primary dark:text-slate-100">
                  {fit.grating}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary dark:text-slate-100 tabular-nums">
                  {fit.error ? (
                    <span className="text-text-secondary dark:text-slate-400 text-xs">{fit.error}</span>
                  ) : fit.redshift !== undefined ? (
                    fit.redshift.toFixed(4)
                  ) : (
                    <span className="text-text-secondary dark:text-slate-400">—</span>
                  )}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary dark:text-slate-100 tabular-nums">
                  {fit.chi2Min !== undefined ? (
                    fit.chi2Min.toFixed(2)
                  ) : (
                    <span className="text-text-secondary dark:text-slate-400">—</span>
                  )}
                </td>
                <td className="py-2 px-3 text-sm text-right text-text-primary dark:text-slate-100 tabular-nums">
                  {fit.confidence !== undefined ? (
                    `${fit.confidence.toFixed(1)}%`
                  ) : (
                    <span className="text-text-secondary dark:text-slate-400">—</span>
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
