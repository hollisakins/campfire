'use client';

import React, { useState, useMemo, useCallback, useRef, useEffect } from 'react';
import Link from 'next/link';
import type { ObjectDetail, ObjectMemberTarget, Spectrum } from '@/lib/types';
import { MEMBER_COLORS, GRATINGS } from '@/lib/types';
import { useInspectionState, type InspectionInitialData } from '@/lib/hooks/useInspectionState';
import { MetricCards } from '@/components/spectra/MetricCards';
import { DownloadButtons } from '@/components/spectra/DownloadButtons';
import { CopyLinkButton } from '@/components/spectra/CopyLinkButton';
import { CoordinateDisplay } from '@/components/spectra/CoordinateDisplay';
import { ShowOnMapLink } from '@/components/map/ShowOnMapLink';
import { FloatingInspectionPanel } from './FloatingInspectionPanel';
import { TileThumbnailWithToggle } from './TileThumbnailWithToggle';
import { ObjectSidebar } from './ObjectSidebar';
import { MultiSpectrumViewer, type SpectrumSource } from './MultiSpectrumViewer';
import { SpectraDetailSection } from './SpectraDetailSection';
import { RedshiftFitSummary } from './RedshiftFitSummary';
import { PhotometrySED } from './PhotometrySED';
import { ObjectComments } from './ObjectComments';
import { NearbyObjects } from './NearbyObjects';
import { StalenessBadge } from './StalenessBadge';

interface UnifiedObjectPageProps {
  object: ObjectDetail;
}

export const UnifiedObjectPage: React.FC<UnifiedObjectPageProps> = ({ object }) => {
  const colors = useMemo(() => {
    const c: Record<string, string> = {};
    object.member_targets.forEach((m, i) => {
      c[m.target_id] = MEMBER_COLORS[i % MEMBER_COLORS.length];
    });
    return c;
  }, [object.member_targets]);

  const programNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const m of object.member_targets) {
      if (!names[m.program_slug]) names[m.program_slug] = m.program_name;
    }
    return names;
  }, [object.member_targets]);

  const [memberOrder, setMemberOrder] = useState<string[]>(
    () => object.member_targets.map(m => m.target_id)
  );

  const [visibility, setVisibility] = useState<Record<string, boolean>>(() => {
    const v: Record<string, boolean> = {};
    object.member_targets.forEach(m => { v[m.target_id] = true; });
    return v;
  });

  const handleVisibilityChange = useCallback((targetId: string, visible: boolean) => {
    setVisibility(prev => ({ ...prev, [targetId]: visible }));
  }, []);

  const handleToggleAll = useCallback((visible: boolean) => {
    setVisibility(prev => {
      const next = { ...prev };
      for (const m of object.member_targets) next[m.target_id] = visible;
      return next;
    });
  }, [object.member_targets]);

  const handleReorder = useCallback((newOrder: string[]) => {
    setMemberOrder(newOrder);
  }, []);

  const orderedMembers = useMemo(() => {
    const map = new Map<string, ObjectMemberTarget>(
      object.member_targets.map(m => [m.target_id, m])
    );
    return memberOrder
      .map(id => map.get(id))
      .filter((m): m is ObjectMemberTarget => m != null);
  }, [object.member_targets, memberOrder]);

  // The spectrum whose redshift_auto sets objects.redshift_auto = highest SNR.
  const selectedSpectrumId = useMemo(() => {
    let best: { id: number; snr: number } | null = null;
    for (const m of object.member_targets) {
      for (const s of m.spectra) {
        if (s.signal_to_noise == null) continue;
        if (!best || s.signal_to_noise > best.snr) {
          best = { id: s.id, snr: s.signal_to_noise };
        }
      }
    }
    return best?.id ?? null;
  }, [object.member_targets]);

  // Section 2 expansion state — default to highest-SNR card expanded.
  const [expandedSpectra, setExpandedSpectra] = useState<Set<number>>(() => {
    const s = new Set<number>();
    if (selectedSpectrumId != null) s.add(selectedSpectrumId);
    return s;
  });

  const handleToggleExpand = useCallback((spectrumId: number) => {
    setExpandedSpectra(prev => {
      const next = new Set(prev);
      if (next.has(spectrumId)) next.delete(spectrumId);
      else next.add(spectrumId);
      return next;
    });
  }, []);

  const handleExpandAll = useCallback(() => {
    const all = new Set<number>();
    for (const m of object.member_targets) for (const s of m.spectra) all.add(s.id);
    setExpandedSpectra(all);
  }, [object.member_targets]);

  const handleCollapseAll = useCallback(() => {
    setExpandedSpectra(new Set());
  }, []);

  // Section 1 filter pills.
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

  // MultiSpectrumViewer source list.
  // Reversed so top sidebar entry = last trace = topmost in Plotly draw order.
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

  // Section 3 — flat list of all member spectra for object-level redshift fit summary.
  const allMemberSpectra: Spectrum[] = useMemo(
    () => object.member_targets.flatMap(m => m.spectra),
    [object.member_targets]
  );

  // Section 6 — exclude object's own member targets from nearby search.
  const excludeTargetIds = useMemo(
    () => object.member_targets.map(m => m.target_id),
    [object.member_targets]
  );

  // Inspection state lives on the parent object (Phase D).
  const initialInspection = useMemo((): InspectionInitialData => ({
    redshift_auto: object.redshift_auto,
    redshift_inspected: object.redshift_inspected,
    redshift_quality: object.redshift_quality,
    last_inspected_at: object.last_inspected_at,
    last_inspected_by: object.last_inspected_by,
    version: object.version,
  }), [object.redshift_auto, object.redshift_inspected, object.redshift_quality, object.last_inspected_at, object.last_inspected_by, object.version]);

  const inspection = useInspectionState(object.id, initialInspection);

  // Reset inspection + expansion when navigating between objects.
  const prevObjectIdRef = useRef(object.id);
  useEffect(() => {
    if (object.id !== prevObjectIdRef.current) {
      prevObjectIdRef.current = object.id;
      inspection.resetState(initialInspection);
      const next = new Set<number>();
      if (selectedSpectrumId != null) next.add(selectedSpectrumId);
      setExpandedSpectra(next);
      setMemberOrder(object.member_targets.map(m => m.target_id));
      const v: Record<string, boolean> = {};
      object.member_targets.forEach(m => { v[m.target_id] = true; });
      setVisibility(v);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [object.id]);

  // Block accidental nav with unsaved inspection state.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (inspection.isDirty()) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cutout shutter colors: all visible members.
  const cutoutMemberColors = useMemo(() => {
    const mc: Record<string, string> = {};
    for (const m of orderedMembers) {
      if (visibility[m.target_id]) mc[m.target_id] = colors[m.target_id];
    }
    return mc;
  }, [orderedMembers, visibility, colors]);

  // Flatten all spectra for the object-level download button.
  const allSpectra: Spectrum[] = useMemo(
    () => object.member_targets.flatMap(m => m.spectra),
    [object.member_targets]
  );

  return (
    <div>
      <div className="flex gap-6 pb-24">
        {/* Desktop sidebar: cutout + members control panel */}
        <div className="hidden lg:block">
          <div className="w-[260px] flex-shrink-0 sticky top-4 max-h-[calc(100vh-6rem)] overflow-y-auto border-r border-border dark:border-slate-700 pr-3">
            <div className="mb-3">
              <TileThumbnailWithToggle
                targetId={object.object_id}
                size={600}
                displaySize={240}
                fov={3.2}
                ra={object.ra}
                dec={object.dec}
                field={object.field}
                linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
                memberColors={cutoutMemberColors}
              />
            </div>
            <ObjectSidebar
              members={orderedMembers}
              colors={colors}
              visibility={visibility}
              onVisibilityChange={handleVisibilityChange}
              onToggleAll={handleToggleAll}
              onReorder={handleReorder}
            />
          </div>
        </div>

        <div className="flex-1 min-w-0">
          {/* Mobile: cutout above content */}
          <div className="lg:hidden mb-4">
            <TileThumbnailWithToggle
              targetId={object.object_id}
              size={600}
              displaySize={200}
              fov={3.2}
              ra={object.ra}
              dec={object.dec}
              field={object.field}
              linkToMap={{ field: object.field, ra: object.ra, dec: object.dec }}
              memberColors={cutoutMemberColors}
            />
          </div>

          {/* === Header === */}
          <div className="mb-6">
            <div className="flex items-center gap-3 mb-2 flex-wrap">
              <h1 className="text-3xl font-bold font-mono text-text-primary dark:text-slate-100">
                {object.object_id}
              </h1>
              <StalenessBadge
                reason={object.staleness_reason}
                lastInspectedAt={object.last_inspected_at}
                lastDataChangeAt={object.last_data_change_at}
              />
              {!object.is_active && (
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-200 dark:bg-slate-700 text-text-secondary dark:text-slate-300">
                  Inactive
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400 mb-3">
              <span>Field:</span>
              <Link
                href={`/nirspec?view=objects&fields=${object.field}`}
                className="inline-flex items-center hover:bg-gray-100 dark:hover:bg-slate-700 px-2 py-1 rounded transition-colors text-text-primary dark:text-slate-100"
              >
                {object.field}
              </Link>
              <span>&middot;</span>
              <span>{object.n_targets} targets</span>
              <span>&middot;</span>
              <span>{object.n_spectra} spectra</span>
            </div>
            <div className="flex items-center gap-4 mb-3">
              <CoordinateDisplay ra={object.ra} dec={object.dec} />
              <ShowOnMapLink ra={object.ra} dec={object.dec} field={object.field} objectId={object.object_id} />
            </div>
            <div className="flex items-end justify-between gap-4 flex-wrap">
              <MetricCards
                maxSnr={object.max_snr}
                redshift={object.redshift}
                redshiftQuality={object.redshift_quality}
                numGratings={object.gratings.length}
              />
              <div className="flex gap-4">
                <DownloadButtons spectra={allSpectra} targetId={object.object_id} />
                <CopyLinkButton
                  targetId={object.object_id}
                  url={`/nirspec/objects/${encodeURIComponent(object.object_id)}`}
                />
              </div>
            </div>
          </div>

          {/* === Section 1: Spectrum Comparison === */}
          <section className="mb-8">
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
            <div className="border border-border dark:border-slate-700 rounded-lg overflow-hidden">
              <MultiSpectrumViewer
                sources={sources}
                grating={selectedGrating}
                redshift={object.redshift}
              />
            </div>
          </section>

          {/* === Section 2: Spectra Detail Cards === */}
          <section className="mb-8">
            <SpectraDetailSection
              orderedMembers={orderedMembers}
              visibility={visibility}
              colors={colors}
              programNames={programNames}
              selectedSpectrumId={selectedSpectrumId}
              objectRedshift={object.redshift}
              expanded={expandedSpectra}
              onToggleExpand={handleToggleExpand}
              onExpandAll={handleExpandAll}
              onCollapseAll={handleCollapseAll}
            />
          </section>

          {/* === Section 3: Redshift Fits === */}
          <section className="mb-8">
            <RedshiftFitSummary
              spectra={allMemberSpectra}
              redshift_auto={object.redshift_auto}
            />
          </section>

          {/* === Section 4: Photometry & SED === */}
          {object.has_photometry && object.photometry && (
            <section className="mb-8">
              <PhotometrySED
                photometry={object.photometry}
                objectId={object.object_id}
                bestRedshift={object.redshift}
              />
            </section>
          )}

          {/* === Section 5: Discussion === */}
          <section className="mb-8">
            <ObjectComments objectDbId={object.id} memberTargets={object.member_targets} />
          </section>

          {/* === Section 6: Nearby Objects === */}
          <section className="mb-8">
            <NearbyObjects
              ra={object.ra}
              dec={object.dec}
              currentTargetId={object.object_id}
              excludeTargetIds={excludeTargetIds}
            />
          </section>
        </div>
      </div>

      <FloatingInspectionPanel
        objectId={object.id}
        ra={object.ra}
        dec={object.dec}
        inspection={inspection}
      />
    </div>
  );
};
