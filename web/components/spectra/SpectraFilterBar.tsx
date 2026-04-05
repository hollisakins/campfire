'use client';

import React, { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { X, Search, ChevronDown, SlidersHorizontal, Map } from 'lucide-react';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { AdvancedFiltersPanel } from './AdvancedFiltersPanel';
import { REDSHIFT_QUALITY } from '@/lib/flags';
import { filtersToURLParams } from '@/lib/utils/url-params';
import type { Program } from '@/lib/types';
import { OBSERVATION_COLORS } from '@/components/map/observation-colors';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';
import type { FilterOptions, FilterMode, SearchScope } from '@/lib/actions/filter-params';
import type { ViewMode } from '@/lib/actions/spectra-types';

// Re-export canonical types for backwards compatibility
export type { FilterOptions, FilterMode, SearchScope };
export type AdvancedFilterOptions = FilterOptions;
export { DEFAULT_FILTERS };

// Search scope options with labels and placeholders
function getSearchScopeOptions(viewMode?: ViewMode): { value: SearchScope; label: string; placeholder: string }[] {
  const idLabel = viewMode === 'objects' ? 'Object ID' : 'Target ID';
  return [
    { value: 'target_id', label: idLabel, placeholder: `Search by ${idLabel}...` },
    { value: 'my_comments', label: 'My Comments', placeholder: 'Search my comments...' },
    { value: 'all_comments', label: 'All Comments', placeholder: 'Search all comments...' },
  ];
}

interface SpectraFilterBarProps {
  filters: AdvancedFilterOptions;
  onFiltersChange: (filters: AdvancedFilterOptions) => void;
  availablePrograms: Program[];
  availableFields: string[];
  availableObservations: string[];
  className?: string;
  isSearchDebouncing?: boolean;
  viewMode?: ViewMode;
}

// Shortcut for "All inspected" quality values (everything except "Not Inspected")
const INSPECTED_QUALITY_VALUES = [1, 2, 3, 4]; // Impossible, Tentative, Probable, Secure

export const SpectraFilterBar: React.FC<SpectraFilterBarProps> = ({
  filters,
  onFiltersChange,
  availablePrograms,
  availableFields,
  availableObservations,
  className = '',
  viewMode,
}) => {
  const router = useRouter();
  // Local state for search input to keep it responsive during typing
  const [localSearch, setLocalSearch] = useState(filters.search);
  // Local state for scope dropdown
  const [scopeDropdownOpen, setScopeDropdownOpen] = useState(false);
  const scopeDropdownRef = useRef<HTMLDivElement>(null);
  // Panel state
  const [panelOpen, setPanelOpen] = useState(false);

  // Track the last value WE sent to the parent to distinguish our echoes from external changes
  const lastSentValueRef = useRef(filters.search);

  // Sync from props ONLY for external changes (e.g., URL navigation), not our own echoes
  useEffect(() => {
    // If this is the value we just sent, it's our own echo - ignore it
    if (filters.search === lastSentValueRef.current) {
      return;
    }
    // External change (e.g., URL navigation) - sync to local state
    setLocalSearch(filters.search);
    lastSentValueRef.current = filters.search;
  }, [filters.search]);

  // Debounce updates to parent - only propagate after 300ms of no typing
  useEffect(() => {
    // Skip if values are already in sync
    if (localSearch === filters.search) return;

    const timer = setTimeout(() => {
      // Update the ref BEFORE calling parent to mark this as "our" change
      lastSentValueRef.current = localSearch;
      onFiltersChange({ ...filters, search: localSearch });
    }, 300);

    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localSearch]); // Intentionally exclude filters and onFiltersChange to prevent loops

  // Close scope dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (scopeDropdownRef.current && !scopeDropdownRef.current.contains(event.target as Node)) {
        setScopeDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Get current scope option
  const searchScopeOptions = getSearchScopeOptions(viewMode);
  const currentScope = searchScopeOptions.find(s => s.value === filters.search_scope) || searchScopeOptions[0];

  // Handle scope change
  const handleScopeChange = (scope: SearchScope) => {
    onFiltersChange({ ...filters, search_scope: scope });
    setScopeDropdownOpen(false);
  };

  const updateFilter = <K extends keyof AdvancedFilterOptions>(
    key: K,
    value: AdvancedFilterOptions[K]
  ) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  // Sort programs by max JWST PID (ascending), then convert to filter options
  const sortedPrograms = [...availablePrograms].sort((a, b) => {
    const maxA = a.jwst_pids?.length ? Math.max(...a.jwst_pids) : 0;
    const maxB = b.jwst_pids?.length ? Math.max(...b.jwst_pids) : 0;
    return maxA - maxB;
  });
  const programOptions: FilterOption[] = sortedPrograms.map((p) => ({
    value: p.slug,
    label: p.program_name || p.slug,
  }));

  // Convert fields to filter options (display in all caps)
  const fieldOptions: FilterOption[] = availableFields.map((f) => ({
    value: f,
    label: f.toUpperCase(),
  }));

  // Convert observations to filter options
  const observationOptions: FilterOption[] = availableObservations.map((o, idx) => ({
    value: o,
    label: o,
    color: OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length],
  }));

  // Convert quality options
  const qualityOptions: FilterOption[] = REDSHIFT_QUALITY.map((q) => ({
    value: q.value,
    label: q.label,
    icon: q.icon,
    color: q.color,
  }));

  // Count panel-only active filters
  const panelFilterCount =
    (filters.coordinate_search !== null ? 1 : 0) +
    (filters.gratings?.length ?? 0) +
    (filters.max_snr_min !== null ? 1 : 0) +
    (filters.max_snr_max !== null ? 1 : 0) +
    (filters.max_exposure_time_min !== null ? 1 : 0) +
    (filters.max_exposure_time_max !== null ? 1 : 0) +
    (filters.spectral_features?.length ?? 0) +
    (filters.object_flags?.length ?? 0) +
    (filters.dq_flags?.length ?? 0);

  // Check if any filters are active (including panel filters)
  const hasActiveFilters =
    filters.programs.length > 0 ||
    filters.fields.length > 0 ||
    filters.observations.length > 0 ||
    filters.redshift_quality.length > 0 ||
    filters.redshift_min !== null ||
    filters.redshift_max !== null ||
    filters.search.length > 0 ||
    panelFilterCount > 0;

  const handleClearAll = () => {
    setLocalSearch('');
    onFiltersChange(DEFAULT_FILTERS);
  };

  const clearPanelFilters = (e: React.MouseEvent) => {
    e.stopPropagation();
    onFiltersChange({
      ...filters,
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
    <div className={`space-y-3 ${className}`}>
      {/* Search bar with scope dropdown */}
      <div className="relative max-w-md flex" ref={scopeDropdownRef}>
        {/* Scope dropdown button */}
        <div className="relative">
          <button
            onClick={() => setScopeDropdownOpen(!scopeDropdownOpen)}
            className="flex items-center gap-1 px-3 py-2 text-sm border border-r-0 border-border dark:border-slate-700 rounded-l-lg bg-card dark:bg-slate-800 text-text-primary dark:text-slate-100 hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
          >
            <span className="whitespace-nowrap">{currentScope.label}</span>
            <ChevronDown className={`w-3.5 h-3.5 transition-transform ${scopeDropdownOpen ? 'rotate-180' : ''}`} />
          </button>
          {/* Dropdown menu */}
          {scopeDropdownOpen && (
            <div className="absolute top-full left-0 mt-1 w-40 bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg z-50 py-1">
              {searchScopeOptions.map((option) => (
                <button
                  key={option.value}
                  onClick={() => handleScopeChange(option.value)}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-card-hover dark:hover:bg-slate-700 transition-colors ${
                    filters.search_scope === option.value
                      ? 'text-primary font-medium'
                      : 'text-text-primary dark:text-slate-100'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}
        </div>
        {/* Search input */}
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary dark:text-slate-400" />
          <input
            type="text"
            value={localSearch}
            onChange={(e) => setLocalSearch(e.target.value)}
            placeholder={currentScope.placeholder}
            className="w-full pl-10 pr-10 py-2 text-sm border border-border dark:border-slate-700 rounded-r-lg bg-background dark:bg-slate-800 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          />
          {/* Show clear button when there's text */}
          {localSearch && (
            <button
              onClick={() => setLocalSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Program filter */}
        <FilterChip
          label="Program"
          options={programOptions}
          selected={filters.programs}
          onChange={(selected) => updateFilter('programs', selected as string[])}
          searchable
          footerLink={{ label: "Browse all programs", href: "/docs/data-products/programs" }}
        />

        {/* Field filter */}
        <FilterChip
          label="Field"
          options={fieldOptions}
          selected={filters.fields}
          onChange={(selected) => updateFilter('fields', selected as string[])}
          searchable
        />

        {/* Observation filter */}
        <FilterChip
          label="Observation"
          options={observationOptions}
          selected={filters.observations}
          onChange={(selected) => updateFilter('observations', selected as string[])}
          searchable
          footerLink={{ label: "Browse all programs", href: "/docs/data-products/programs" }}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />

        {/* Redshift range filter */}
        <RangeFilterChip
          label="Redshift"
          min={filters.redshift_min}
          max={filters.redshift_max}
          onChange={(min, max) => {
            onFiltersChange({ ...filters, redshift_min: min, redshift_max: max });
          }}
          minBound={0}
          maxBound={15}
          step={0.1}
          precision={2}
        />

        {/* Quality filter with "All inspected" shortcut */}
        <FilterChip
          label="Quality"
          options={qualityOptions}
          selected={filters.redshift_quality}
          onChange={(selected) => updateFilter('redshift_quality', selected as number[])}
          shortcut={{ label: 'All inspected', values: INSPECTED_QUALITY_VALUES }}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />

        {/* Advanced Filters button */}
        <button
          onClick={() => setPanelOpen(true)}
          className={`
            inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium
            border transition-all duration-200
            ${panelFilterCount > 0
              ? 'bg-primary/10 border-primary text-primary'
              : 'bg-card dark:bg-slate-800 border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200'
            }
          `}
        >
          <SlidersHorizontal className="w-4 h-4" />
          <span>Advanced</span>
          {panelFilterCount > 0 && (
            <span className="px-1.5 py-0.5 text-[10px] font-bold rounded bg-primary text-white">
              {panelFilterCount}
            </span>
          )}
          {panelFilterCount > 0 ? (
            <X
              className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer"
              onClick={clearPanelFilters}
            />
          ) : (
            <ChevronDown className="w-3.5 h-3.5" />
          )}
        </button>

        {/* Show on Map button */}
        <button
          onClick={() => {
            const filterParams = filtersToURLParams(filters);
            // If exactly one field is selected, also set it as the map field param
            if (filters.fields.length === 1) {
              filterParams.set('field', filters.fields[0]);
            }
            const qs = filterParams.toString();
            router.push(`/map${qs ? `?${qs}` : ''}`);
          }}
          className="inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium border border-border dark:border-slate-700 bg-card dark:bg-slate-800 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200 transition-all duration-200"
          title="Show filtered objects on map"
        >
          <Map className="w-4 h-4" />
          <span>Map</span>
        </button>

        {/* Clear all button */}
        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              Clear all
            </button>
          </>
        )}
      </div>

      {/* Advanced Filters Panel */}
      <AdvancedFiltersPanel
        isOpen={panelOpen}
        onClose={() => setPanelOpen(false)}
        filters={filters}
        onFiltersChange={onFiltersChange}
        viewMode={viewMode}
      />
    </div>
  );
};
