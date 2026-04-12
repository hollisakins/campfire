'use client';

import React, { useMemo, useEffect } from 'react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { GratingDetails } from '@/components/spectra/GratingDetails';
import { SpectrumPlot } from '@/components/spectra/SpectrumPlot';
import { RedshiftFitSummary } from '@/components/spectra/RedshiftFitSummary';
import { RedshiftFitPlot } from '@/components/spectra/RedshiftFitPlot';
import { SEDPlotViewer } from '@/components/spectra/SEDPlotViewer';
import { StickyInspectionBar } from '@/components/spectra/StickyInspectionBar';
import { useInspectionState } from '@/lib/hooks/useInspectionState';
import type { ObjectMemberTarget } from '@/lib/types';
import { GRATINGS } from '@/lib/types';

interface TargetTabProps {
  target: ObjectMemberTarget;
  initialGrating?: string;
  color: string;
  /** Ref callback: parent sets this to check dirty state before tab switch */
  onDirtyRef?: React.MutableRefObject<(() => boolean) | null>;
}

export const TargetTab: React.FC<TargetTabProps> = ({
  target,
  initialGrating,
  color,
  onDirtyRef,
}) => {
  // Sort spectra by standard grating order
  const sortedSpectra = useMemo(() =>
    [...target.spectra].sort((a, b) => {
      const order = GRATINGS as readonly string[];
      return order.indexOf(a.grating) - order.indexOf(b.grating);
    }),
    [target.spectra]
  );

  // Determine default tab
  const defaultTab = useMemo(() => {
    if (initialGrating) {
      const match = sortedSpectra.find(
        s => s.grating.toLowerCase() === initialGrating.toLowerCase()
      );
      if (match) return match.grating.toLowerCase();
    }
    return sortedSpectra[0]?.grating.toLowerCase() || 'redshift';
  }, [initialGrating, sortedSpectra]);

  const initialRedshift = target.redshift_inspected ?? target.redshift_auto;

  // Inspection state — powers the sticky bar at the bottom
  const inspection = useInspectionState(target.id, {
    redshift_auto: target.redshift_auto,
    redshift_inspected: target.redshift_inspected,
    redshift_quality: target.redshift_quality,
    spectral_features: target.spectral_features,
    dq_flags: target.dq_flags,
    last_inspected_at: target.last_inspected_at,
    last_inspected_by: target.last_inspected_by,
  });

  // Expose isDirty to parent for tab-switch confirmation
  useEffect(() => {
    if (onDirtyRef) {
      onDirtyRef.current = inspection.isDirty;
    }
    return () => {
      if (onDirtyRef) onDirtyRef.current = null;
    };
  }, [onDirtyRef, inspection.isDirty]);

  return (
    <div>
      <Tabs defaultValue={defaultTab}>
        {/* Target header */}
        <div className="mb-4">
          <div className="flex items-center gap-2 mb-1">
            <div
              className="w-3 h-3 rounded-full flex-shrink-0"
              style={{ backgroundColor: color }}
            />
            <h2 className="text-xl font-bold font-mono text-text-primary dark:text-slate-100">
              {target.target_id}
            </h2>
          </div>
          <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400 mb-3">
            <span>{target.program_name}</span>
            <span>&middot;</span>
            <span>{target.observation}</span>
            {target.redshift != null && (
              <>
                <span>&middot;</span>
                <span className="font-mono">z = {target.redshift.toFixed(4)}</span>
              </>
            )}
          </div>

          {/* Grating sub-tabs */}
          <TabsList>
            {sortedSpectra.map(spec => (
              <TabsTrigger key={spec.grating} value={spec.grating.toLowerCase()}>
                {spec.grating}
              </TabsTrigger>
            ))}
            <TabsTrigger value="redshift">REDSHIFT</TabsTrigger>
            {target.has_sed_plot && (
              <TabsTrigger value="photometry">PHOTOMETRY</TabsTrigger>
            )}
          </TabsList>
        </div>

        {/* Tab content */}
        <div className="mt-4">
          {/* Grating tabs */}
          {sortedSpectra.map(spec => (
            <TabsContent key={spec.grating} value={spec.grating.toLowerCase()}>
              <GratingDetails spectrum={spec} />
              <SpectrumPlot
                fitsPath={spec.fits_path}
                grating={spec.grating}
                initialRedshift={initialRedshift}
              />
            </TabsContent>
          ))}

          {/* Redshift tab */}
          <TabsContent value="redshift">
            <div className="space-y-4">
              <RedshiftFitSummary
                spectra={target.spectra}
                redshift_auto={target.redshift_auto}
              />
              {sortedSpectra.map((spec, index) => (
                <details key={index} className="group" open={index === 0}>
                  <summary className="cursor-pointer list-none">
                    <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg p-4 hover:bg-gray-50 dark:hover:bg-slate-700 transition-colors">
                      <div className="flex items-center justify-between">
                        <h3 className="text-lg font-semibold text-text-primary dark:text-slate-100">
                          {spec.grating} Redshift Fit
                        </h3>
                        <svg
                          className="w-5 h-5 text-text-secondary dark:text-slate-400 transition-transform group-open:rotate-180"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                        </svg>
                      </div>
                    </div>
                  </summary>
                  <div className="mt-2">
                    <RedshiftFitPlot
                      fitsPath={spec.fits_path}
                      grating={spec.grating}
                      initialRedshift={initialRedshift}
                    />
                  </div>
                </details>
              ))}
            </div>
          </TabsContent>

          {/* Photometry tab */}
          {target.has_sed_plot && (
            <TabsContent value="photometry">
              <SEDPlotViewer targetId={target.target_id} />
            </TabsContent>
          )}
        </div>
      </Tabs>

      {/* Sticky inspection bar — always visible at bottom of target tab */}
      <StickyInspectionBar inspection={inspection} />
    </div>
  );
};
