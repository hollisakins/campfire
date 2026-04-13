'use client';

import React, { useState, useMemo } from 'react';
import type { ObjectDetail, ObjectMemberTarget } from '@/lib/types';
import { GRATINGS } from '@/lib/types';
import { MultiSpectrumViewer, type SpectrumSource } from './MultiSpectrumViewer';
import { NearbyObjects } from './NearbyObjects';
import { ObjectComments } from './ObjectComments';

interface OverviewTabProps {
  object: ObjectDetail;
  colors: Record<string, string>;
  /** Members in user-controlled order (from sidebar drag) */
  orderedMembers: ObjectMemberTarget[];
  /** Visibility state (from sidebar checkboxes) */
  visibility: Record<string, boolean>;
  /** Map from program slug to human-readable name */
  programNames: Record<string, string>;
}

export const OverviewTab: React.FC<OverviewTabProps> = ({
  object,
  colors,
  orderedMembers,
  visibility,
  programNames,
}) => {
  const sortedGratings = useMemo(() =>
    GRATINGS.filter(g => object.gratings.includes(g)),
    [object.gratings]
  );

  const [selectedGrating, setSelectedGrating] = useState<string | null>(null);
  const [selectedProgram, setSelectedProgram] = useState<string | null>(null);

  const filteredMembers = useMemo(() =>
    selectedProgram
      ? orderedMembers.filter(m => m.program_slug === selectedProgram)
      : orderedMembers,
    [orderedMembers, selectedProgram]
  );

  // Derive sources for the MultiSpectrumViewer
  // Reversed so top sidebar entry = last trace = topmost in Plotly
  const sources: SpectrumSource[] = useMemo(() =>
    [...filteredMembers]
      .reverse()
      .filter(m => visibility[m.target_id])
      .flatMap(m =>
        m.spectra
          .filter(s => !selectedGrating || s.grating === selectedGrating)
          .map(s => ({
            fitsPath: s.fits_path,
            label: selectedGrating ? m.target_id : `${m.target_id} (${s.grating})`,
            color: colors[m.target_id],
            visible: true,
          }))
      ),
    [filteredMembers, visibility, selectedGrating, colors]
  );

  const excludeTargetIds = useMemo(
    () => object.member_targets.map(m => m.target_id),
    [object.member_targets]
  );

  return (
    <div>
      {/* Spectrum comparison header + filter toggles */}
      <div className="flex flex-wrap gap-5 items-center mb-4">
        <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
          Spectrum Comparison{selectedGrating ? ` — ${selectedGrating}` : ''}
        </h2>
        {object.programs.length > 1 && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-text-secondary dark:text-slate-400">Program:</span>
            <div className="flex items-center bg-gray-100 dark:bg-slate-700 rounded-md p-0.5">
              <button
                onClick={() => setSelectedProgram(null)}
                className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                  selectedProgram === null
                    ? 'bg-white dark:bg-slate-600 text-text-primary dark:text-slate-100 shadow-sm'
                    : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }`}
              >
                All
              </button>
              {object.programs.map(p => (
                <button
                  key={p}
                  onClick={() => setSelectedProgram(selectedProgram === p ? null : p)}
                  className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                    selectedProgram === p
                      ? 'bg-white dark:bg-slate-600 text-text-primary dark:text-slate-100 shadow-sm'
                      : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                  }`}
                >
                  {programNames[p] || p}
                </button>
              ))}
            </div>
          </div>
        )}
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-secondary dark:text-slate-400">Grating:</span>
          <div className="flex items-center bg-gray-100 dark:bg-slate-700 rounded-md p-0.5">
            <button
              onClick={() => setSelectedGrating(null)}
              className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                selectedGrating === null
                  ? 'bg-white dark:bg-slate-600 text-text-primary dark:text-slate-100 shadow-sm'
                  : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
              }`}
            >
              All
            </button>
            {sortedGratings.map(g => (
              <button
                key={g}
                onClick={() => setSelectedGrating(selectedGrating === g ? null : g)}
                className={`px-2.5 py-1 text-xs font-medium font-mono rounded transition-colors ${
                  g === selectedGrating
                    ? 'bg-white dark:bg-slate-600 text-text-primary dark:text-slate-100 shadow-sm'
                    : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }`}
              >
                {g}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Spectrum comparison viewer */}
      <div className="mb-6 border border-border dark:border-slate-700 rounded-lg overflow-hidden">
        <MultiSpectrumViewer
          sources={sources}
          grating={selectedGrating}
          redshift={object.best_redshift}
        />
      </div>

      {/* Object-level comments */}
      <div className="mb-6">
        <ObjectComments objectDbId={object.id} memberTargets={object.member_targets} />
      </div>

      {/* Nearby Objects */}
      <NearbyObjects
        ra={object.ra}
        dec={object.dec}
        currentTargetId={object.object_id}
        excludeTargetIds={excludeTargetIds}
      />
    </div>
  );
};
