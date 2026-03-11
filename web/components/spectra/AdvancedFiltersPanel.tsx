'use client';

import React, { useEffect, useState } from 'react';
import { X, Info, Check, ChevronDown } from 'lucide-react';
import { InlineMultiFilter } from '@/components/ui/InlineMultiFilter';
import { InlineRange } from '@/components/ui/InlineRange';
import { parseCoordinates, convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import {
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
  REDSHIFT_QUALITY,
} from '@/lib/flags';
import type { AdvancedFilterOptions } from './SpectraFilterBar';
import { GRATINGS, type Program } from '@/lib/types';
import { OBSERVATION_COLORS } from '@/components/map/observation-colors';

interface FilterOption {
  value: string | number;
  label: string;
  icon?: string;
  color?: string;
}

interface AdvancedFiltersPanelProps {
  isOpen: boolean;
  onClose: () => void;
  filters: AdvancedFilterOptions;
  onFiltersChange: (filters: AdvancedFilterOptions) => void;
  /** When true, show basic filters (programs, observation, quality, redshift) above advanced sections and hide position search */
  showBasicFilters?: boolean;
  /** Available programs for the program filter (required when showBasicFilters is true) */
  availablePrograms?: Program[];
  /** Available observations for the observation filter (required when showBasicFilters is true) */
  availableObservations?: string[];
}

export function AdvancedFiltersPanel({
  isOpen,
  onClose,
  filters,
  onFiltersChange,
  showBasicFilters = false,
  availablePrograms = [],
  availableObservations = [],
}: AdvancedFiltersPanelProps) {
  // Local state for coordinate search form
  const [coordInput, setCoordInput] = useState('');
  const [radiusInput, setRadiusInput] = useState('1');
  const [unitInput, setUnitInput] = useState<'degrees' | 'arcmin' | 'arcsec'>('arcmin');
  const [validationError, setValidationError] = useState<string | null>(null);

  // Sync local state with filter value
  useEffect(() => {
    if (filters.coordinate_search) {
      setCoordInput(`${filters.coordinate_search.ra.toFixed(6)} ${filters.coordinate_search.dec.toFixed(6)}`);
      setRadiusInput(filters.coordinate_search.radius.toString());
      setUnitInput(filters.coordinate_search.radius_unit);
    } else {
      setCoordInput('');
      setRadiusInput('1');
      setUnitInput('arcmin');
    }
  }, [filters.coordinate_search]);

  // Validate coordinates on input change
  useEffect(() => {
    if (coordInput.trim() === '') {
      setValidationError(null);
      return;
    }
    const parsed = parseCoordinates(coordInput);
    if (parsed === null) {
      setValidationError('Invalid format. Use: "150.5 -2.3" or "10h02m30s -02d18m00s"');
    } else {
      setValidationError(null);
    }
  }, [coordInput]);

  // Apply coordinate search
  const handleApplyCoordSearch = () => {
    if (coordInput.trim() === '') {
      onFiltersChange({ ...filters, coordinate_search: null });
      return;
    }

    const parsed = parseCoordinates(coordInput);
    if (parsed === null) {
      setValidationError('Invalid coordinate format');
      return;
    }

    const radius = parseFloat(radiusInput);
    if (isNaN(radius) || radius <= 0) {
      setValidationError('Radius must be a positive number');
      return;
    }

    // Validate max radius (1 degree)
    const radiusDegrees = convertRadiusToDegrees(radius, unitInput);
    if (radiusDegrees > 1) {
      setValidationError('Maximum search radius is 1 degree');
      return;
    }

    onFiltersChange({
      ...filters,
      coordinate_search: {
        ra: parsed.ra,
        dec: parsed.dec,
        radius,
        radius_unit: unitInput,
      },
    });
  };

  // Clear coordinate search
  const handleClearCoordSearch = () => {
    setCoordInput('');
    setRadiusInput('1');
    setUnitInput('arcmin');
    setValidationError(null);
    onFiltersChange({ ...filters, coordinate_search: null });
  };

  // Handle Enter key in inputs
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleApplyCoordSearch();
    }
  };

  const isCoordSearchActive = filters.coordinate_search !== null;

  // Close panel on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [onClose]);

  // Prevent body scroll when panel is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  // Filter options
  const gratingOptions: FilterOption[] = GRATINGS.map(g => ({ value: g, label: g }));

  const spectralFeatureOptions: FilterOption[] = SPECTRAL_FEATURES.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const objectFlagOptions: FilterOption[] = OBJECT_FLAGS.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  const dqFlagOptions: FilterOption[] = DQ_FLAGS.map(f => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  // Count panel-only active filters
  const basicFilterCount = showBasicFilters
    ? (filters.programs?.length ?? 0) +
      (filters.observations?.length ?? 0) +
      (filters.redshift_quality?.length ?? 0) +
      (filters.redshift_min !== null ? 1 : 0) +
      (filters.redshift_max !== null ? 1 : 0)
    : 0;

  const panelFilterCount =
    basicFilterCount +
    (filters.coordinate_search !== null ? 1 : 0) +
    (filters.gratings?.length ?? 0) +
    (filters.max_snr_min !== null ? 1 : 0) +
    (filters.max_snr_max !== null ? 1 : 0) +
    (filters.max_exposure_time_min !== null ? 1 : 0) +
    (filters.max_exposure_time_max !== null ? 1 : 0) +
    (filters.spectral_features?.length ?? 0) +
    (filters.object_flags?.length ?? 0) +
    (filters.dq_flags?.length ?? 0);

  // Collapsible section state for basic filters (collapsed by default, auto-expand if filter active)
  const [programsExpanded, setProgramsExpanded] = useState(false);
  const [observationsExpanded, setObservationsExpanded] = useState(false);

  // Program, observation, and quality options for basic filters
  const programOptions: FilterOption[] = availablePrograms.map((p) => ({
    value: p.program_id,
    label: p.program_name ? `${p.program_name} (${p.program_id})` : `Program ${p.program_id}`,
  }));

  const observationOptions: FilterOption[] = availableObservations.map((o, idx) => ({
    value: o,
    label: o,
    color: OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length],
  }));

  const qualityOptions: FilterOption[] = REDSHIFT_QUALITY.map((q) => ({
    value: q.value,
    label: q.label,
    icon: q.icon,
    color: q.color,
  }));

  const clearPanelFilters = () => {
    const basicClear = showBasicFilters
      ? {
          programs: [] as number[],
          observations: [] as string[],
          redshift_quality: [] as number[],
          redshift_min: null,
          redshift_max: null,
        }
      : {};

    onFiltersChange({
      ...filters,
      ...basicClear,
      coordinate_search: null,
      gratings: [],
      gratings_mode: 'any',
      max_snr_min: null,
      max_snr_max: null,
      max_exposure_time_min: null,
      max_exposure_time_max: null,
      spectral_features: [],
      spectral_features_mode: 'any',
      object_flags: [],
      object_flags_mode: 'any',
      dq_flags: [],
      dq_flags_mode: 'any',
    });
  };

  return (
    <div
      className={`
        fixed top-0 left-0 right-0 bottom-0 z-[10000] transition-all duration-300 ease-out
        ${isOpen ? 'visible' : 'invisible pointer-events-none'}
      `}
      style={{ position: 'fixed' }}
    >
      {/* Backdrop */}
      <div
        className={`
          absolute top-0 left-0 right-0 bottom-0 bg-slate-900/60 dark:bg-black/70
          transition-opacity duration-300
          ${isOpen ? 'opacity-100' : 'opacity-0'}
        `}
        onClick={onClose}
      />

      {/* Panel */}
      <div
        className={`
          absolute right-0 top-0 bottom-0 w-[420px] max-w-[90vw]
          bg-background dark:bg-slate-900 border-l border-border dark:border-slate-700
          shadow-2xl flex flex-col
          transition-transform duration-300 ease-out
          ${isOpen ? 'translate-x-0 pointer-events-auto' : 'translate-x-full'}
        `}
      >
        {/* Panel Header */}
        <div className="flex items-center justify-between p-4 border-b border-border dark:border-slate-700 bg-card dark:bg-slate-800">
          <div>
            <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100">
              {showBasicFilters ? 'Filters' : 'Advanced Filters'}
            </h2>
            <p className="text-xs text-text-secondary dark:text-slate-400 mt-0.5">
              {showBasicFilters ? 'Filter objects shown on the map' : 'Multi-value filters and spectra properties'}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Panel Content */}
        <div className="flex-1 overflow-y-auto">
          {/* Basic Filters (shown on map page) */}
          {showBasicFilters && (
            <>
              {/* Programs — collapsible */}
              <div className="border-b border-border dark:border-slate-700">
                <button
                  onClick={() => setProgramsExpanded(!programsExpanded)}
                  className="w-full flex items-center justify-between px-4 py-3 text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500 hover:bg-card-hover dark:hover:bg-slate-800/50 transition-colors"
                >
                  <span>
                    Programs
                    {(filters.programs?.length ?? 0) > 0 && (
                      <span className="ml-1.5 text-[10px] font-bold text-primary">({filters.programs.length})</span>
                    )}
                  </span>
                  <ChevronDown className={`w-4 h-4 transition-transform ${programsExpanded ? 'rotate-180' : ''}`} />
                </button>
                {programsExpanded && (
                  <div className="px-4 pb-4">
                    <InlineMultiFilter
                      label=""
                      options={programOptions}
                      selected={filters.programs ?? []}
                      onChange={(s) => onFiltersChange({ ...filters, programs: s as number[] })}
                      mode="any"
                      onModeChange={() => {}}
                    />
                  </div>
                )}
              </div>

              {/* Observations — collapsible */}
              <div className="border-b border-border dark:border-slate-700">
                <button
                  onClick={() => setObservationsExpanded(!observationsExpanded)}
                  className="w-full flex items-center justify-between px-4 py-3 text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500 hover:bg-card-hover dark:hover:bg-slate-800/50 transition-colors"
                >
                  <span className="flex flex-col items-start">
                    <span>
                      Observations
                      {(filters.observations?.length ?? 0) > 0 && (
                        <span className="ml-1.5 text-[10px] font-bold text-primary">({filters.observations.length})</span>
                      )}
                    </span>
                    <span className="text-[10px] font-normal normal-case tracking-normal text-gray-400 dark:text-slate-500">Filters objects and map shutters</span>
                  </span>
                  <ChevronDown className={`w-4 h-4 transition-transform ${observationsExpanded ? 'rotate-180' : ''}`} />
                </button>
                {observationsExpanded && (
                  <div className="px-4 pb-4">
                    <InlineMultiFilter
                      label=""
                      options={observationOptions}
                      selected={filters.observations ?? []}
                      onChange={(s) => onFiltersChange({ ...filters, observations: s as string[] })}
                      mode="any"
                      onModeChange={() => {}}
                    />
                  </div>
                )}
              </div>

              {/* Redshift Quality */}
              <div className="p-4 border-b border-border dark:border-slate-700">
                <InlineMultiFilter
                  label="Redshift Quality"
                  options={qualityOptions}
                  selected={filters.redshift_quality ?? []}
                  onChange={(s) => onFiltersChange({ ...filters, redshift_quality: s as number[] })}
                  mode="any"
                  onModeChange={() => {}}
                />
              </div>

              {/* Redshift Range */}
              <div className="p-4 border-b border-border dark:border-slate-700">
                <InlineRange
                  label="Redshift"
                  description="Filter by redshift range"
                  min={filters.redshift_min ?? null}
                  max={filters.redshift_max ?? null}
                  onChange={(min, max) => onFiltersChange({ ...filters, redshift_min: min, redshift_max: max })}
                  minBound={0}
                  maxBound={15}
                  step={0.1}
                  precision={2}
                />
              </div>
            </>
          )}

          {/* Position Search Section - Inline Form (hidden on map view) */}
          {!showBasicFilters && (
            <div className="p-4 border-b border-border dark:border-slate-700">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500">
                  Position Search
                </h3>
                {isCoordSearchActive && (
                  <div className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
                    <Check className="w-3.5 h-3.5" />
                    <span>Active</span>
                  </div>
                )}
              </div>

              <div className="space-y-3">
                {/* Coordinate input */}
                <div>
                  <label className="block text-xs text-text-secondary dark:text-slate-400 mb-1">
                    Coordinates (RA Dec)
                  </label>
                  <input
                    type="text"
                    value={coordInput}
                    onChange={(e) => setCoordInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="150.5 -2.3  or  10h02m30s -02d18m00s"
                    className={`w-full px-3 py-2 text-sm border rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent font-mono
                      ${validationError ? 'border-red-500 dark:border-red-600' : 'border-border dark:border-slate-600'}
                    `}
                  />
                  {validationError && (
                    <p className="text-xs text-red-500 dark:text-red-400 mt-1">{validationError}</p>
                  )}
                </div>

                {/* Radius input with units */}
                <div>
                  <label className="block text-xs text-text-secondary dark:text-slate-400 mb-1">
                    Search radius (max 1 degree)
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={radiusInput}
                      onChange={(e) => setRadiusInput(e.target.value)}
                      onKeyDown={handleKeyDown}
                      placeholder="1"
                      min="0"
                      step="0.1"
                      className="w-24 px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                    />
                    <select
                      value={unitInput}
                      onChange={(e) => setUnitInput(e.target.value as 'degrees' | 'arcmin' | 'arcsec')}
                      className="flex-1 px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
                    >
                      <option value="arcsec">arcseconds</option>
                      <option value="arcmin">arcminutes</option>
                      <option value="degrees">degrees</option>
                    </select>
                  </div>
                </div>

                {/* Action buttons */}
                <div className="flex gap-2 pt-1">
                  <button
                    onClick={handleClearCoordSearch}
                    className="flex-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 border border-border dark:border-slate-600 rounded-md hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
                  >
                    Clear
                  </button>
                  <button
                    onClick={handleApplyCoordSearch}
                    disabled={validationError !== null && coordInput.trim() !== ''}
                    className="flex-1 px-3 py-1.5 text-sm bg-primary text-white rounded-md hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Apply
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Gratings Section */}
          <div className="p-4 border-b border-border dark:border-slate-700">
            <InlineMultiFilter
              label="Gratings"
              options={gratingOptions}
              selected={filters.gratings ?? []}
              onChange={(s) => onFiltersChange({ ...filters, gratings: s as string[] })}
              mode={filters.gratings_mode}
              onModeChange={(m) => onFiltersChange({ ...filters, gratings_mode: m })}
            />
          </div>

          {/* Spectra-Specific Section */}
          <div className="p-4 border-b border-border dark:border-slate-700">
            <div className="flex items-start gap-2 mb-4 p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
              <Info className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
              <div className="text-xs text-amber-700 dark:text-amber-300">
                <strong>Spectra-specific values:</strong> These apply per-spectrum, not per-object.
                Currently filters to objects where <em>any</em> spectrum matches.
              </div>
            </div>

            <InlineRange
              label="Max S/N"
              description="Signal-to-noise ratio of the best spectrum"
              min={filters.max_snr_min ?? null}
              max={filters.max_snr_max ?? null}
              onChange={(min, max) => onFiltersChange({ ...filters, max_snr_min: min, max_snr_max: max })}
              minBound={0}
              maxBound={150}
              step={1}
              precision={0}
            />

            <div className="mt-4">
              <InlineRange
                label="Max Exp. Time"
                description="Maximum exposure time across all gratings (seconds)"
                min={filters.max_exposure_time_min ?? null}
                max={filters.max_exposure_time_max ?? null}
                onChange={(min, max) => onFiltersChange({ ...filters, max_exposure_time_min: min, max_exposure_time_max: max })}
                minBound={0}
                maxBound={50000}
                step={100}
                precision={0}
              />
            </div>
          </div>

          {/* Object Flags Section */}
          <div className="p-4 border-b border-border dark:border-slate-700">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500 mb-4">
              Object Classification
            </h3>
            <div className="space-y-5">
              <InlineMultiFilter
                label="Object Type"
                options={objectFlagOptions}
                selected={filters.object_flags ?? []}
                onChange={(s) => onFiltersChange({ ...filters, object_flags: s as number[] })}
                mode={filters.object_flags_mode}
                onModeChange={(m) => onFiltersChange({ ...filters, object_flags_mode: m })}
              />
              <InlineMultiFilter
                label="Spectral Features"
                options={spectralFeatureOptions}
                selected={filters.spectral_features ?? []}
                onChange={(s) => onFiltersChange({ ...filters, spectral_features: s as number[] })}
                mode={filters.spectral_features_mode}
                onModeChange={(m) => onFiltersChange({ ...filters, spectral_features_mode: m })}
              />
            </div>
          </div>

          {/* Data Quality Section */}
          <div className="p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500 mb-4">
              Data Quality
            </h3>
            <InlineMultiFilter
              label="Quality Flags"
              options={dqFlagOptions}
              selected={filters.dq_flags ?? []}
              onChange={(s) => onFiltersChange({ ...filters, dq_flags: s as number[] })}
              mode={filters.dq_flags_mode}
              onModeChange={(m) => onFiltersChange({ ...filters, dq_flags_mode: m })}
            />
          </div>
        </div>

        {/* Panel Footer */}
        <div className="p-4 border-t border-border dark:border-slate-700 bg-card dark:bg-slate-800">
          <div className="flex gap-3">
            <button
              onClick={clearPanelFilters}
              disabled={panelFilterCount === 0}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
            >
              Clear Filters{panelFilterCount > 0 ? ` (${panelFilterCount})` : ''}
            </button>
            <button
              onClick={onClose}
              className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-white hover:bg-primary-hover shadow-sm hover:shadow transition-all duration-200"
            >
              Done
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
