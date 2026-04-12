'use client';

import React, { useState, useMemo, useCallback } from 'react';
import type { ObjectDetail, ObjectMemberTarget } from '@/lib/types';
import { GRATINGS, MEMBER_COLORS } from '@/lib/types';
import { MemberTargetsTable } from './MemberTargetsTable';
import { MultiSpectrumViewer, type SpectrumSource } from './MultiSpectrumViewer';
import { TileThumbnailWithToggle } from './TileThumbnailWithToggle';
import { NearbyObjects } from './NearbyObjects';
import { ObjectComments } from './ObjectComments';

interface OverviewTabProps {
  object: ObjectDetail;
  colors: Record<string, string>;
  onTargetClick: (targetId: string) => void;
}

export const OverviewTab: React.FC<OverviewTabProps> = ({
  object,
  colors,
  onTargetClick,
}) => {
  // Sort available gratings by standard order
  const sortedGratings = useMemo(() =>
    GRATINGS.filter(g => object.gratings.includes(g)),
    [object.gratings]
  );

  const [selectedGrating, setSelectedGrating] = useState<string | null>(null);
  const [selectedProgram, setSelectedProgram] = useState<string | null>(null);

  // User-controlled row order (controls z-order in plot)
  const [memberOrder, setMemberOrder] = useState<string[]>(
    () => object.member_targets.map(m => m.target_id)
  );

  const [visibility, setVisibility] = useState<Record<string, boolean>>(() => {
    const vis: Record<string, boolean> = {};
    object.member_targets.forEach((m) => {
      vis[m.target_id] = true;
    });
    return vis;
  });

  const handleVisibilityChange = (targetId: string, visible: boolean) => {
    setVisibility(prev => ({ ...prev, [targetId]: visible }));
  };

  const orderedMembers = useMemo(() => {
    const memberMap = new Map<string, ObjectMemberTarget>(
      object.member_targets.map(m => [m.target_id, m])
    );
    return memberOrder
      .map(id => memberMap.get(id))
      .filter((m): m is ObjectMemberTarget => m != null);
  }, [object.member_targets, memberOrder]);

  const filteredMembers = useMemo(() =>
    selectedProgram
      ? orderedMembers.filter(m => m.program_slug === selectedProgram)
      : orderedMembers,
    [orderedMembers, selectedProgram]
  );

  const handleReorder = useCallback((newOrder: string[]) => {
    setMemberOrder(newOrder);
  }, []);

  const handleToggleAll = (visible: boolean) => {
    setVisibility(prev => {
      const next = { ...prev };
      for (const m of filteredMembers) {
        next[m.target_id] = visible;
      }
      return next;
    });
  };

  // Derive sources for the MultiSpectrumViewer
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

  const filteredMemberColors = useMemo(() => {
    const fc: Record<string, string> = {};
    for (const m of filteredMembers) {
      if (visibility[m.target_id]) {
        fc[m.target_id] = colors[m.target_id];
      }
    }
    return fc;
  }, [filteredMembers, visibility, colors]);

  const excludeTargetIds = object.member_targets.map(m => m.target_id);

  return (
    <div>
      {/* Cutout + Filters */}
      <div className="flex gap-6 items-start mb-4">
        <div className="flex-1">
          {/* Program and Grating selectors */}
          <div className="flex flex-wrap gap-5 items-center">
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
                      {p}
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
        </div>

        {/* Cutout */}
        <div className="flex-shrink-0" style={{ width: '300px' }}>
          <TileThumbnailWithToggle
            targetId={object.object_id}
            size={600}
            displaySize={300}
            fov={3.2}
            ra={object.ra}
            dec={object.dec}
            field={object.field}
            linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
            memberColors={filteredMemberColors}
          />
        </div>
      </div>

      {/* Member targets table + spectrum viewer */}
      <div className="space-y-4">
        <MemberTargetsTable
          members={filteredMembers}
          objectId={object.object_id}
          selectedGrating={selectedGrating}
          visibility={visibility}
          colors={colors}
          onVisibilityChange={handleVisibilityChange}
          onToggleAll={handleToggleAll}
          onReorder={handleReorder}
          onTargetClick={onTargetClick}
        />

        <div>
          <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100 mb-3">
            Spectrum Comparison{selectedGrating ? ` — ${selectedGrating}` : ''}
          </h2>
          <MultiSpectrumViewer
            sources={sources}
            grating={selectedGrating}
            redshift={object.best_redshift}
          />
        </div>
      </div>

      {/* Object-level comments */}
      <div className="mt-6">
        <ObjectComments objectDbId={object.id} />
      </div>

      {/* Nearby Objects */}
      <div className="mt-6">
        <NearbyObjects
          ra={object.ra}
          dec={object.dec}
          currentTargetId={object.object_id}
          excludeTargetIds={excludeTargetIds}
        />
      </div>
    </div>
  );
};
