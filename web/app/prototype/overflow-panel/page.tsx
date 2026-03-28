'use client';

import { useState, useMemo, useRef, useEffect } from 'react';
import {
  MOCK_SPECTRA,
  MOCK_PROGRAMS,
  applyFiltersToMockData,
  type MockFilterOptions,
  type FilterMode,
} from '@/lib/mocks/spectra-mock-data';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
  getQualityDef,
} from '@/lib/flags';
import { ChevronDown, X, Columns3, RotateCcw, Check, SlidersHorizontal, Info } from 'lucide-react';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';

/**
 * Approach D: Filter Bar + Slide-Out Panel (Enhanced)
 *
 * Main bar: Program, Field, Observation, Quality, Redshift
 * Panel: Complex multi-value filters + spectra-specific values
 */

const GRATINGS = ['PRISM', 'G140M', 'G235M', 'G395M'];

interface FilterState extends MockFilterOptions {
  gratings_mode: FilterMode;
  spectral_features_mode: FilterMode;
  object_flags_mode: FilterMode;
  dq_flags_mode: FilterMode;
}

const DEFAULT_FILTERS: FilterState = {
  programs: [],
  fields: [],
  gratings: [],
  gratings_mode: 'any',
  observations: [],
  redshift_quality: [],
  redshift_min: null,
  redshift_max: null,
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
  inspected_only: null,
  search: '',
};

const ALL_COLUMNS = [
  { id: 'target_id', label: 'Target ID', alwaysVisible: true },
  { id: 'field', label: 'Field', defaultVisible: true },
  { id: 'observation', label: 'Observation', defaultVisible: false },
  { id: 'gratings', label: 'Gratings', defaultVisible: true },
  { id: 'redshift', label: 'Redshift', defaultVisible: true },
  { id: 'quality', label: 'Quality', defaultVisible: true },
  { id: 'max_snr', label: 'Max S/N', defaultVisible: true },
  { id: 'exptime', label: 'Exp. Time', defaultVisible: false },
];

function darkenColor(hex: string, percent: number): string {
  const color = hex.replace('#', '');
  const num = parseInt(color, 16);
  let r = (num >> 16) & 0xff;
  let g = (num >> 8) & 0xff;
  let b = num & 0xff;

  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  let h = 0, s = 0, l = (max + min) / 2;

  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    switch (max) {
      case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
      case g: h = ((b - r) / d + 2) / 6; break;
      case b: h = ((r - g) / d + 4) / 6; break;
    }
  }

  s = Math.min(1, s * 1.2);
  l = l * (1 - percent / 100);

  const hue2rgb = (p: number, q: number, t: number) => {
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1/6) return p + (q - p) * 6 * t;
    if (t < 1/2) return q;
    if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
    return p;
  };

  let rOut, gOut, bOut;
  if (s === 0) {
    rOut = gOut = bOut = l;
  } else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    rOut = hue2rgb(p, q, h + 1/3);
    gOut = hue2rgb(p, q, h);
    bOut = hue2rgb(p, q, h - 1/3);
  }

  const rHex = Math.round(rOut * 255);
  const gHex = Math.round(gOut * 255);
  const bHex = Math.round(bOut * 255);

  return '#' + ((rHex << 16) | (gHex << 8) | bHex).toString(16).padStart(6, '0');
}

interface FilterOption {
  value: string | number;
  label: string;
  icon?: string;
  color?: string;
}

// =============================================================================
// Multi-Value Filter (inline version for panel)
// =============================================================================

interface InlineMultiFilterProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
  mode: FilterMode;
  onModeChange: (mode: FilterMode) => void;
}

function InlineMultiFilter({
  label,
  options,
  selected,
  onChange,
  mode,
  onModeChange,
}: InlineMultiFilterProps) {
  const toggle = (value: string | number) => {
    if (selected.includes(value)) {
      onChange(selected.filter(v => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  const hasSelection = selected.length > 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="text-sm font-medium text-text-primary dark:text-slate-200">
          {label}
        </label>
        {/* Always show mode selector to prevent layout shift */}
        <div className={`flex gap-0.5 bg-slate-100 dark:bg-slate-800 rounded-md p-0.5 transition-opacity duration-200 ${hasSelection ? 'opacity-100' : 'opacity-40 pointer-events-none'}`}>
          {(['any', 'all', 'none'] as FilterMode[]).map((m) => (
            <button
              key={m}
              onClick={() => onModeChange(m)}
              disabled={!hasSelection}
              className={`
                px-2.5 py-1 text-xs font-medium rounded transition-all duration-200
                ${mode === m
                  ? m === 'any'
                    ? 'bg-primary text-white shadow-sm'
                    : m === 'all'
                      ? 'bg-green-500 text-white shadow-sm'
                      : 'bg-red-500 text-white shadow-sm'
                  : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                }
              `}
            >
              {m === 'any' ? 'Any' : m === 'all' ? 'All' : 'None'}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => {
          const isSelected = selected.includes(option.value);
          return (
            <button
              key={option.value}
              onClick={() => toggle(option.value)}
              className={`
                inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm
                border transition-all duration-200
                ${isSelected
                  ? 'border-transparent text-gray-900 dark:text-slate-100 shadow-sm'
                  : 'border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 hover:border-slate-300 dark:hover:border-slate-600'
                }
              `}
              style={
                isSelected && option.color
                  ? { backgroundColor: `${option.color}60`, borderColor: darkenColor(option.color, 30) }
                  : isSelected
                    ? { backgroundColor: 'rgb(192 38 211 / 0.4)', borderColor: darkenColor('#c026d3', 30) }
                    : undefined
              }
            >
              {option.icon && <span>{option.icon}</span>}
              <span>{option.label}</span>
            </button>
          );
        })}
      </div>
      {/* Always reserve space for description to prevent layout shift */}
      <p className={`mt-2 text-xs text-text-secondary dark:text-slate-500 h-4 transition-opacity duration-200 ${hasSelection ? 'opacity-100' : 'opacity-0'}`}>
        {mode === 'any' && 'Show objects with any of the selected'}
        {mode === 'all' && 'Show objects with all of the selected'}
        {mode === 'none' && 'Exclude objects with any of the selected'}
        {!hasSelection && '\u00A0'} {/* Non-breaking space to maintain height */}
      </p>
    </div>
  );
}

// =============================================================================
// Range Input (inline version for panel)
// =============================================================================

interface InlineRangeProps {
  label: string;
  description?: string;
  min: number | null;
  max: number | null;
  onChange: (min: number | null, max: number | null) => void;
  minBound: number;
  maxBound: number;
  step: number;
  precision?: number;
}

function InlineRange({ label, description, min, max, onChange, minBound, maxBound, step, precision = 1 }: InlineRangeProps) {
  const [minValue, setMinValue] = useState(min?.toString() ?? '');
  const [maxValue, setMaxValue] = useState(max?.toString() ?? '');

  useEffect(() => {
    setMinValue(min?.toString() ?? '');
    setMaxValue(max?.toString() ?? '');
  }, [min, max]);

  const handleMinBlur = () => {
    const val = minValue === '' ? null : parseFloat(minValue);
    if (val !== null && (isNaN(val) || val < minBound || val > maxBound)) return;
    onChange(val, max);
  };

  const handleMaxBlur = () => {
    const val = maxValue === '' ? null : parseFloat(maxValue);
    if (val !== null && (isNaN(val) || val < minBound || val > maxBound)) return;
    onChange(min, val);
  };

  const isActive = min !== null || max !== null;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="text-sm font-medium text-text-primary dark:text-slate-200">
          {label}
        </label>
        {isActive && (
          <button
            onClick={() => onChange(null, null)}
            className="text-xs text-text-secondary dark:text-slate-400 hover:text-primary transition-colors"
          >
            Clear
          </button>
        )}
      </div>
      {description && (
        <p className="text-xs text-text-secondary dark:text-slate-500 mb-2">{description}</p>
      )}
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <input
            type="number"
            value={minValue}
            onChange={(e) => setMinValue(e.target.value)}
            onBlur={handleMinBlur}
            onKeyDown={(e) => e.key === 'Enter' && handleMinBlur()}
            placeholder={`Min (${minBound})`}
            step={step}
            className={`
              w-full px-3 py-2 text-sm border rounded-lg bg-background dark:bg-slate-900
              text-text-primary dark:text-slate-200 transition-all duration-200
              focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
              ${isActive ? 'border-primary/50' : 'border-border dark:border-slate-700'}
            `}
          />
        </div>
        <span className="text-sm text-text-secondary dark:text-slate-400">to</span>
        <div className="flex-1">
          <input
            type="number"
            value={maxValue}
            onChange={(e) => setMaxValue(e.target.value)}
            onBlur={handleMaxBlur}
            onKeyDown={(e) => e.key === 'Enter' && handleMaxBlur()}
            placeholder={`Max (${maxBound})`}
            step={step}
            className={`
              w-full px-3 py-2 text-sm border rounded-lg bg-background dark:bg-slate-900
              text-text-primary dark:text-slate-200 transition-all duration-200
              focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
              ${isActive ? 'border-primary/50' : 'border-border dark:border-slate-700'}
            `}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Simple Filter Chip (for main bar)
// =============================================================================

interface SimpleFilterProps {
  label: string;
  options: FilterOption[];
  selected: (string | number)[];
  onChange: (selected: (string | number)[]) => void;
}

function SimpleFilter({ label, options, selected, onChange }: SimpleFilterProps) {
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const toggle = (value: string | number) => {
    if (selected.includes(value)) {
      onChange(selected.filter(v => v !== value));
    } else {
      onChange([...selected, value]);
    }
  };

  const handleClear = (e: React.MouseEvent) => {
    e.stopPropagation();
    onChange([]);
  };

  const isActive = selected.length > 0;

  return (
    <div ref={ref} className="relative inline-block flex-shrink-0">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium
          border transition-all duration-200
          ${isActive
            ? 'bg-primary/10 border-primary text-primary'
            : 'bg-card dark:bg-slate-800 border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200'
          }
        `}
      >
        <span>{label}</span>
        {isActive && (
          <span className="px-1.5 py-0.5 text-[10px] font-bold rounded bg-primary text-white">
            {selected.length}
          </span>
        )}
        {isActive ? (
          <X className="w-3.5 h-3.5 hover:text-primary-hover cursor-pointer" onClick={handleClear} />
        ) : (
          <ChevronDown className={`w-3.5 h-3.5 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
        )}
      </button>

      {isOpen && (
        <div className="absolute z-50 mt-1 min-w-[200px] max-w-[280px] max-h-[400px] overflow-y-auto bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg animate-in fade-in slide-in-from-top-2 duration-200">
          <div className="p-1">
            {options.map((option) => {
              const isSelected = selected.includes(option.value);
              return (
                <button
                  key={option.value}
                  onClick={() => toggle(option.value)}
                  className="w-full flex items-center gap-3 px-3 py-2 text-sm text-left hover:bg-card-hover dark:hover:bg-slate-700 rounded-md transition-colors"
                >
                  <div className={`
                    w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-200
                    ${isSelected ? 'bg-primary border-primary scale-110' : 'border-border dark:border-slate-600'}
                  `}>
                    {isSelected && <Check className="w-3 h-3 text-white" />}
                  </div>
                  {option.icon && <span className="text-sm">{option.icon}</span>}
                  <span className={isSelected ? 'text-text-primary dark:text-slate-100' : 'text-text-secondary dark:text-slate-400'}>
                    {option.label}
                  </span>
                </button>
              );
            })}
          </div>
          {selected.length > 0 && (
            <div className="border-t border-border dark:border-slate-700 p-2">
              <button
                onClick={() => { onChange([]); setIsOpen(false); }}
                className="w-full px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card dark:hover:bg-slate-700 rounded-md text-left transition-colors"
              >
                Clear all
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Column Visibility
// =============================================================================

interface ColumnVisibilityProps {
  columns: typeof ALL_COLUMNS;
  visibility: Record<string, boolean>;
  onChange: (visibility: Record<string, boolean>) => void;
}

function ColumnVisibility({ columns, visibility, onChange }: ColumnVisibilityProps) {
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setIsOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const toggle = (colId: string) => {
    const col = columns.find(c => c.id === colId);
    if (col?.alwaysVisible) return;
    onChange({ ...visibility, [colId]: !visibility[colId] });
  };

  const resetToDefaults = () => {
    const defaults: Record<string, boolean> = {};
    columns.forEach(c => {
      defaults[c.id] = c.alwaysVisible || c.defaultVisible !== false;
    });
    onChange(defaults);
  };

  const visibleCount = Object.values(visibility).filter(Boolean).length;

  return (
    <div ref={ref} className="relative inline-block flex-shrink-0">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium border transition-all duration-200 bg-card dark:bg-slate-800 border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:border-text-secondary dark:hover:border-slate-600 hover:text-text-primary dark:hover:text-slate-200"
      >
        <Columns3 className="w-4 h-4" />
        <span className="px-1.5 py-0.5 text-xs bg-slate-100 dark:bg-slate-700 rounded">
          {visibleCount}
        </span>
      </button>

      {isOpen && (
        <div className="absolute right-0 z-50 mt-1 w-64 max-h-[400px] overflow-y-auto bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg animate-in fade-in slide-in-from-top-2 duration-200">
          <div className="sticky top-0 bg-background dark:bg-slate-800 border-b border-border dark:border-slate-700 p-2">
            <button
              onClick={resetToDefaults}
              className="flex items-center gap-1.5 px-2 py-1 text-xs text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card dark:hover:bg-slate-700 rounded transition-colors"
            >
              <RotateCcw className="w-3 h-3" />
              Reset to defaults
            </button>
          </div>
          <div className="p-2 space-y-1">
            {columns.map(col => {
              const isVisible = visibility[col.id] ?? true;
              const isLocked = col.alwaysVisible;
              return (
                <button
                  key={col.id}
                  onClick={() => toggle(col.id)}
                  disabled={isLocked}
                  className={`
                    w-full flex items-center gap-3 px-3 py-2 rounded-md text-sm text-left transition-all duration-200
                    ${isLocked
                      ? 'opacity-60 cursor-not-allowed bg-slate-50 dark:bg-slate-800'
                      : isVisible
                        ? 'bg-primary/10 dark:bg-primary/20 text-text-primary dark:text-slate-100'
                        : 'text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700'
                    }
                  `}
                >
                  <div className={`
                    w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 transition-all duration-200
                    ${isVisible ? 'bg-primary border-primary' : 'border-border dark:border-slate-600'}
                  `}>
                    {isVisible && <Check className="w-3 h-3 text-white" />}
                  </div>
                  <span className="flex-1">{col.label}</span>
                  {isLocked && (
                    <span className="text-xs text-text-secondary dark:text-slate-500">required</span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Main Page
// =============================================================================

export default function OverflowPanelPage() {
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [panelOpen, setPanelOpen] = useState(false);

  const [columnVisibility, setColumnVisibility] = useState<Record<string, boolean>>(() => {
    if (typeof window === 'undefined') {
      const defaults: Record<string, boolean> = {};
      ALL_COLUMNS.forEach(c => { defaults[c.id] = c.alwaysVisible || c.defaultVisible !== false; });
      return defaults;
    }
    try {
      const stored = localStorage.getItem('prototype-overflow-panel-columns');
      if (stored) return JSON.parse(stored);
    } catch {}
    const defaults: Record<string, boolean> = {};
    ALL_COLUMNS.forEach(c => { defaults[c.id] = c.alwaysVisible || c.defaultVisible !== false; });
    return defaults;
  });

  useEffect(() => {
    localStorage.setItem('prototype-overflow-panel-columns', JSON.stringify(columnVisibility));
  }, [columnVisibility]);

  // Close panel on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPanelOpen(false);
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, []);

  // Filter options
  const fields = [...new Set(MOCK_SPECTRA.map(s => s.field))].sort();
  const observations = [...new Set(MOCK_SPECTRA.map(s => s.observation).filter(Boolean) as string[])].sort();

  const programOptions: FilterOption[] = MOCK_PROGRAMS.map(p => ({
    value: p.slug,
    label: p.program_name || p.slug,
  }));

  const fieldOptions: FilterOption[] = fields.map(f => ({ value: f, label: f }));
  const gratingOptions: FilterOption[] = GRATINGS.map(g => ({ value: g, label: g }));
  const observationOptions: FilterOption[] = observations.map(o => ({ value: o, label: o }));

  const qualityOptions: FilterOption[] = REDSHIFT_QUALITY.map(q => ({
    value: q.value,
    label: q.label,
    icon: q.icon,
    color: q.color,
  }));

  const featureOptions: FilterOption[] = SPECTRAL_FEATURES.map(f => ({
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

  const filteredData = useMemo(() => applyFiltersToMockData(MOCK_SPECTRA, filters), [filters]);

  const [page, setPage] = useState(0);
  const pageSize = 12;
  const paginatedData = filteredData.slice(page * pageSize, (page + 1) * pageSize);
  const totalPages = Math.ceil(filteredData.length / pageSize);

  useEffect(() => { setPage(0); }, [filters]);

  // Count panel-only active filters (not in main bar)
  const panelFilterCount =
    (filters.gratings?.length ?? 0) +
    (filters.max_snr_min !== null ? 1 : 0) +
    (filters.max_snr_max !== null ? 1 : 0) +
    (filters.spectral_features?.length ?? 0) +
    (filters.object_flags?.length ?? 0) +
    (filters.dq_flags?.length ?? 0);

  const hasAnyActiveFilters =
    (filters.programs?.length ?? 0) > 0 ||
    (filters.fields?.length ?? 0) > 0 ||
    (filters.observations?.length ?? 0) > 0 ||
    (filters.redshift_quality?.length ?? 0) > 0 ||
    filters.redshift_min !== null || filters.redshift_max !== null ||
    panelFilterCount > 0;

  const visibleColumns = ALL_COLUMNS.filter(c => columnVisibility[c.id]);

  return (
    <div className="space-y-4 relative">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-text-primary dark:text-slate-100">
            D: Slide-Out Panel (Enhanced)
          </h1>
          <p className="text-sm text-text-secondary dark:text-slate-400">
            Primary filters in bar. Advanced filters + spectra-specific values in panel.
          </p>
        </div>
        <ColumnVisibility
          columns={ALL_COLUMNS}
          visibility={columnVisibility}
          onChange={setColumnVisibility}
        />
      </div>

      {/* Main Filter Bar */}
      <div className="flex items-center gap-2 p-3 bg-card dark:bg-slate-800 rounded-lg border border-border dark:border-slate-700">
        {/* Primary filters */}
        <SimpleFilter
          label="Program"
          options={programOptions}
          selected={filters.programs ?? []}
          onChange={(s) => setFilters({ ...filters, programs: s as string[] })}
        />
        <SimpleFilter
          label="Field"
          options={fieldOptions}
          selected={filters.fields ?? []}
          onChange={(s) => setFilters({ ...filters, fields: s as string[] })}
        />
        <SimpleFilter
          label="Observation"
          options={observationOptions}
          selected={filters.observations ?? []}
          onChange={(s) => setFilters({ ...filters, observations: s as string[] })}
        />
        <SimpleFilter
          label="Quality"
          options={qualityOptions}
          selected={filters.redshift_quality ?? []}
          onChange={(s) => setFilters({ ...filters, redshift_quality: s as number[] })}
        />
        <RangeFilterChip
          label="Redshift"
          min={filters.redshift_min ?? null}
          max={filters.redshift_max ?? null}
          onChange={(min, max) => setFilters({ ...filters, redshift_min: min, redshift_max: max })}
          minBound={0}
          maxBound={15}
          step={0.1}
          precision={2}
        />

        {/* Divider */}
        <div className="h-7 w-px bg-border dark:bg-slate-700 mx-1 flex-shrink-0" />

        {/* Advanced Filters button - fixed height to prevent layout shift */}
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
              onClick={(e) => {
                e.stopPropagation();
                setFilters({
                  ...filters,
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
              }}
            />
          ) : (
            <ChevronDown className="w-3.5 h-3.5" />
          )}
        </button>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Clear all */}
        {hasAnyActiveFilters && (
          <button
            onClick={() => setFilters(DEFAULT_FILTERS)}
            className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors flex-shrink-0"
          >
            <X className="w-3.5 h-3.5" />
            Clear all
          </button>
        )}
      </div>

      {/* Results count */}
      <div className="text-sm text-text-secondary dark:text-slate-400">
        <strong className="text-text-primary dark:text-slate-100">{filteredData.length}</strong> of {MOCK_SPECTRA.length} objects
      </div>

      {/* Table */}
      <div className="border border-border dark:border-slate-700 rounded-lg overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 dark:bg-slate-800 border-b border-border dark:border-slate-700">
                {visibleColumns.map(col => (
                  <th key={col.id} className="px-4 py-3 text-left font-medium text-text-secondary dark:text-slate-400 whitespace-nowrap">
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paginatedData.length === 0 ? (
                <tr>
                  <td colSpan={visibleColumns.length} className="px-4 py-8 text-center text-text-secondary dark:text-slate-400">
                    No objects match filters
                  </td>
                </tr>
              ) : (
                paginatedData.map((row, idx) => {
                  const quality = getQualityDef(row.redshift_quality);
                  return (
                    <tr
                      key={row.id}
                      className={`
                        border-b border-border dark:border-slate-700 last:border-0
                        ${idx % 2 === 0 ? 'bg-background dark:bg-slate-900' : 'bg-slate-50/50 dark:bg-slate-800/50'}
                        hover:bg-slate-100/50 dark:hover:bg-slate-700/30 transition-colors
                      `}
                    >
                      {visibleColumns.map(col => (
                        <td key={col.id} className="px-4 py-2.5 whitespace-nowrap">
                          {col.id === 'target_id' && (
                            <span className="font-mono text-sm text-primary">{row.target_id}</span>
                          )}
                          {col.id === 'field' && (
                            <span className="text-text-primary dark:text-slate-200">{row.field}</span>
                          )}
                          {col.id === 'observation' && (
                            <span className="text-text-secondary dark:text-slate-400">{row.observation || '-'}</span>
                          )}
                          {col.id === 'gratings' && (
                            <div className="flex gap-1">
                              {row.spectra.map((s: { grating: string }) => (
                                <span key={s.grating} className="px-1.5 py-0.5 text-xs rounded bg-slate-100 dark:bg-slate-700 text-text-secondary dark:text-slate-300">
                                  {s.grating}
                                </span>
                              ))}
                            </div>
                          )}
                          {col.id === 'redshift' && (
                            <span className="text-text-primary dark:text-slate-200">
                              {row.redshift?.toFixed(4) ?? <span className="text-text-secondary dark:text-slate-500">-</span>}
                            </span>
                          )}
                          {col.id === 'quality' && (
                            <span className="inline-flex items-center gap-1">
                              <span>{quality.icon}</span>
                              <span className="text-xs text-text-secondary dark:text-slate-400">{quality.short}</span>
                            </span>
                          )}
                          {col.id === 'max_snr' && (
                            <span className="text-text-primary dark:text-slate-200">
                              {row.max_snr?.toFixed(1) ?? '-'}
                            </span>
                          )}
                          {col.id === 'exptime' && (
                            <span className="text-text-secondary dark:text-slate-400">
                              {(row as typeof row & { total_exptime?: number }).total_exptime?.toFixed(0) ?? '-'}
                            </span>
                          )}
                        </td>
                      ))}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-text-secondary dark:text-slate-400">
            Page {page + 1} of {totalPages}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="px-3 py-1.5 rounded border border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Previous
            </button>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page === totalPages - 1}
              className="px-3 py-1.5 rounded border border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {/* Slide-Out Panel with smooth animation */}
      {/* Using fixed positioning with explicit top/left/right/bottom to ensure full viewport coverage */}
      <div
        className={`
          fixed top-0 left-0 right-0 bottom-0 z-[100] transition-all duration-300 ease-out
          ${panelOpen ? 'visible' : 'invisible pointer-events-none'}
        `}
        style={{ position: 'fixed' }}
      >
        {/* Backdrop - solid color to fully cover the page */}
        <div
          className={`
            absolute top-0 left-0 right-0 bottom-0 bg-slate-900/60 dark:bg-black/70
            transition-opacity duration-300
            ${panelOpen ? 'opacity-100' : 'opacity-0'}
          `}
          onClick={() => setPanelOpen(false)}
        />

        {/* Panel */}
        <div
          className={`
            absolute right-0 top-0 bottom-0 w-[420px] max-w-[90vw]
            bg-background dark:bg-slate-900 border-l border-border dark:border-slate-700
            shadow-2xl flex flex-col
            transition-transform duration-300 ease-out
            ${panelOpen ? 'translate-x-0 pointer-events-auto' : 'translate-x-full'}
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
              onClick={() => setPanelOpen(false)}
              className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          {/* Panel Content */}
          <div className="flex-1 overflow-y-auto">
            {/* Gratings Section */}
            <div className="p-4 border-b border-border dark:border-slate-700">
              <InlineMultiFilter
                label="Gratings"
                options={gratingOptions}
                selected={filters.gratings ?? []}
                onChange={(s) => setFilters({ ...filters, gratings: s as string[] })}
                mode={filters.gratings_mode}
                onModeChange={(m) => setFilters({ ...filters, gratings_mode: m })}
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

              <div className="space-y-5">
                <InlineRange
                  label="Max S/N"
                  description="Signal-to-noise ratio of the best spectrum"
                  min={filters.max_snr_min ?? null}
                  max={filters.max_snr_max ?? null}
                  onChange={(min, max) => setFilters({ ...filters, max_snr_min: min, max_snr_max: max })}
                  minBound={0}
                  maxBound={150}
                  step={1}
                  precision={0}
                />

                {/* Placeholder for exposure time - future feature */}
                <div className="opacity-60">
                  <InlineRange
                    label="Exposure Time (s)"
                    description="Total integration time per grating"
                    min={null}
                    max={null}
                    onChange={() => {}}
                    minBound={0}
                    maxBound={100000}
                    step={100}
                    precision={0}
                  />
                  <p className="mt-1 text-xs text-text-secondary dark:text-slate-500 italic">
                    Coming soon - requires database migration
                  </p>
                </div>
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
                  onChange={(s) => setFilters({ ...filters, object_flags: s as number[] })}
                  mode={filters.object_flags_mode}
                  onModeChange={(m) => setFilters({ ...filters, object_flags_mode: m })}
                />
                <InlineMultiFilter
                  label="Spectral Features"
                  options={featureOptions}
                  selected={filters.spectral_features ?? []}
                  onChange={(s) => setFilters({ ...filters, spectral_features: s as number[] })}
                  mode={filters.spectral_features_mode}
                  onModeChange={(m) => setFilters({ ...filters, spectral_features_mode: m })}
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
                onChange={(s) => setFilters({ ...filters, dq_flags: s as number[] })}
                mode={filters.dq_flags_mode}
                onModeChange={(m) => setFilters({ ...filters, dq_flags_mode: m })}
              />
            </div>
          </div>

          {/* Panel Footer */}
          <div className="p-4 border-t border-border dark:border-slate-700 bg-card dark:bg-slate-800">
            <div className="flex gap-3">
              <button
                onClick={() => {
                  setFilters({
                    ...filters,
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
                }}
                disabled={panelFilterCount === 0}
                className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg border border-border dark:border-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200"
              >
                Clear Filters{panelFilterCount > 0 ? ` (${panelFilterCount})` : ''}
              </button>
              <button
                onClick={() => setPanelOpen(false)}
                className="flex-1 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-white hover:bg-primary-hover shadow-sm hover:shadow transition-all duration-200"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
