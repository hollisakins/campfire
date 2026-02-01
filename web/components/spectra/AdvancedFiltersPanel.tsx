'use client';

import React, { useEffect, useState } from 'react';
import { X, Info, Check } from 'lucide-react';
import { InlineMultiFilter } from '@/components/ui/InlineMultiFilter';
import { InlineRange } from '@/components/ui/InlineRange';
import { parseCoordinates, convertRadiusToDegrees } from '@/lib/utils/coordinate-parser';
import {
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
} from '@/lib/flags';
import type { AdvancedFilterOptions } from './SpectraFilterBar';

const GRATINGS = ['PRISM', 'G140M', 'G235M', 'G395M'];

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
}

export function AdvancedFiltersPanel({
  isOpen,
  onClose,
  filters,
  onFiltersChange,
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
  const panelFilterCount =
    (filters.coordinate_search !== null ? 1 : 0) +
    (filters.gratings?.length ?? 0) +
    (filters.max_snr_min !== null ? 1 : 0) +
    (filters.max_snr_max !== null ? 1 : 0) +
    (filters.spectral_features?.length ?? 0) +
    (filters.object_flags?.length ?? 0) +
    (filters.dq_flags?.length ?? 0);

  const clearPanelFilters = () => {
    onFiltersChange({
      ...filters,
      coordinate_search: null,
      gratings: [],
      gratings_mode: 'any',
      max_snr_min: null,
      max_snr_max: null,
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
        fixed top-0 left-0 right-0 bottom-0 z-[100] transition-all duration-300 ease-out
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
              Advanced Filters
            </h2>
            <p className="text-xs text-text-secondary dark:text-slate-400 mt-0.5">
              Multi-value filters and spectra properties
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
          {/* Position Search Section - Inline Form */}
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
