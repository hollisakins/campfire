import Link from 'next/link';
import { PanelRight, Check, AlertCircle, Layers, SlidersHorizontal, X, ChevronDown } from 'lucide-react';

/**
 * Filter UI Prototype - Implementation Summary
 *
 * This page documents the final filter bar design ready for production implementation.
 * The design solves horizontal overflow issues while maintaining usability.
 */
export default function PrototypePage() {
  return (
    <div className="space-y-8 max-w-4xl">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">
          Filter Bar: Slide-Out Panel Design
        </h1>
        <p className="mt-2 text-text-secondary dark:text-slate-400">
          Final prototype for the CAMPFIRE spectra filter bar. Primary filters remain in the main bar
          while advanced filters are accessible via a slide-out panel.
        </p>
        <Link
          href="/prototype/overflow-panel"
          className="inline-flex items-center gap-2 mt-4 px-4 py-2 rounded-lg bg-primary text-white hover:bg-primary-hover transition-colors"
        >
          <PanelRight className="w-4 h-4" />
          View Live Demo
        </Link>
      </div>

      {/* Problem & Solution */}
      <div className="grid gap-4 md:grid-cols-2">
        <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
          <div className="flex items-center gap-2 mb-2">
            <AlertCircle className="w-5 h-5 text-red-600 dark:text-red-400" />
            <h3 className="font-semibold text-red-800 dark:text-red-300">Problem</h3>
          </div>
          <p className="text-sm text-red-700 dark:text-red-400">
            The current filter bar uses <code className="px-1 py-0.5 bg-red-100 dark:bg-red-800 rounded">flex-wrap</code>,
            causing filters to shift to the next line when selections add badges. This makes click targets
            unpredictable and frustrating on laptop screens (1280-1440px).
          </p>
        </div>
        <div className="p-4 rounded-lg bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800">
          <div className="flex items-center gap-2 mb-2">
            <Check className="w-5 h-5 text-green-600 dark:text-green-400" />
            <h3 className="font-semibold text-green-800 dark:text-green-300">Solution</h3>
          </div>
          <p className="text-sm text-green-700 dark:text-green-400">
            Split filters into a stable main bar (5 primary filters) and a slide-out panel (advanced filters).
            Fixed button heights prevent layout shift. Consistent badge/icon patterns across all buttons.
          </p>
        </div>
      </div>

      {/* Architecture */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-text-primary dark:text-slate-100 flex items-center gap-2">
          <Layers className="w-5 h-5 text-primary" />
          Filter Architecture
        </h2>

        {/* Main Bar */}
        <div className="p-4 rounded-lg border border-border dark:border-slate-700 bg-card dark:bg-slate-800">
          <h3 className="font-medium text-text-primary dark:text-slate-100 mb-3">
            Main Filter Bar (Always Visible)
          </h3>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[
              { name: 'Program', type: 'Dropdown multi-select', notes: 'JWST program names' },
              { name: 'Field', type: 'Dropdown multi-select', notes: 'COSMOS, UDS, etc.' },
              { name: 'Observation', type: 'Dropdown multi-select', notes: 'Specific observation names' },
              { name: 'Quality', type: 'Dropdown multi-select', notes: 'Redshift quality flags (icons + colors)' },
              { name: 'Redshift', type: 'Range input', notes: 'Min/max with popover' },
              { name: 'Advanced', type: 'Panel trigger', notes: 'Opens slide-out panel' },
            ].map((filter) => (
              <div key={filter.name} className="p-3 rounded-lg bg-slate-50 dark:bg-slate-700/50">
                <div className="font-medium text-sm text-text-primary dark:text-slate-200">{filter.name}</div>
                <div className="text-xs text-primary mt-0.5">{filter.type}</div>
                <div className="text-xs text-text-secondary dark:text-slate-400 mt-1">{filter.notes}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Panel */}
        <div className="p-4 rounded-lg border border-border dark:border-slate-700 bg-card dark:bg-slate-800">
          <h3 className="font-medium text-text-primary dark:text-slate-100 mb-3">
            Slide-Out Panel (On Demand)
          </h3>
          <div className="space-y-4">
            {[
              {
                section: 'Gratings',
                filters: [{ name: 'Gratings', type: 'Multi-select with Any/All/None mode', notes: 'PRISM, G140M, G235M, G395M' }],
              },
              {
                section: 'Spectra-Specific Values',
                filters: [
                  { name: 'Max S/N', type: 'Range input', notes: 'Signal-to-noise ratio' },
                  { name: 'Exposure Time', type: 'Range input', notes: 'Coming soon - requires DB migration' },
                ],
                warning: 'These are per-spectrum values. Currently filters to objects where ANY spectrum matches.',
              },
              {
                section: 'Object Classification',
                filters: [
                  { name: 'Object Type', type: 'Multi-select with mode', notes: 'Star, Galaxy, AGN, etc.' },
                  { name: 'Spectral Features', type: 'Multi-select with mode', notes: 'Emission lines, absorption, etc.' },
                ],
              },
              {
                section: 'Data Quality',
                filters: [{ name: 'Quality Flags', type: 'Multi-select with mode', notes: 'DQ bit flags' }],
              },
            ].map((group) => (
              <div key={group.section}>
                <h4 className="text-xs font-semibold uppercase tracking-wider text-text-secondary dark:text-slate-500 mb-2">
                  {group.section}
                </h4>
                {group.warning && (
                  <p className="text-xs text-amber-600 dark:text-amber-400 mb-2 italic">{group.warning}</p>
                )}
                <div className="grid gap-2 sm:grid-cols-2">
                  {group.filters.map((filter) => (
                    <div key={filter.name} className="p-2 rounded bg-slate-50 dark:bg-slate-700/50">
                      <div className="font-medium text-sm text-text-primary dark:text-slate-200">{filter.name}</div>
                      <div className="text-xs text-primary">{filter.type}</div>
                      <div className="text-xs text-text-secondary dark:text-slate-400 mt-0.5">{filter.notes}</div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* UI Patterns */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-text-primary dark:text-slate-100 flex items-center gap-2">
          <SlidersHorizontal className="w-5 h-5 text-primary" />
          UI Patterns
        </h2>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Button States */}
          <div className="p-4 rounded-lg border border-border dark:border-slate-700">
            <h3 className="font-medium text-text-primary dark:text-slate-100 mb-3">Filter Button States</h3>
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <div className="inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium border border-border dark:border-slate-600 bg-card dark:bg-slate-700 text-text-secondary dark:text-slate-400">
                  <span>Filter</span>
                  <ChevronDown className="w-3.5 h-3.5" />
                </div>
                <span className="text-xs text-text-secondary dark:text-slate-400">Inactive: border + chevron</span>
              </div>
              <div className="flex items-center gap-3">
                <div className="inline-flex items-center gap-1.5 px-3 h-8 rounded-full text-sm font-medium border border-primary bg-primary/10 text-primary">
                  <span>Filter</span>
                  <span className="px-1.5 py-0.5 text-[10px] font-bold rounded bg-primary text-white">2</span>
                  <X className="w-3.5 h-3.5" />
                </div>
                <span className="text-xs text-text-secondary dark:text-slate-400">Active: badge + X to clear</span>
              </div>
            </div>
          </div>

          {/* Mode Selector */}
          <div className="p-4 rounded-lg border border-border dark:border-slate-700">
            <h3 className="font-medium text-text-primary dark:text-slate-100 mb-3">Multi-Value Mode Selector</h3>
            <div className="space-y-2">
              <div className="flex gap-0.5 bg-slate-100 dark:bg-slate-700 rounded-md p-0.5 w-fit">
                <span className="px-2.5 py-1 text-xs font-medium rounded bg-primary text-white">Any</span>
                <span className="px-2.5 py-1 text-xs font-medium text-text-secondary dark:text-slate-400">All</span>
                <span className="px-2.5 py-1 text-xs font-medium text-text-secondary dark:text-slate-400">None</span>
              </div>
              <p className="text-xs text-text-secondary dark:text-slate-400">
                <strong>Any:</strong> Match objects with any selected value<br />
                <strong>All:</strong> Match objects with all selected values<br />
                <strong>None:</strong> Exclude objects with any selected value
              </p>
            </div>
          </div>
        </div>

        {/* Key Implementation Details */}
        <div className="p-4 rounded-lg border border-border dark:border-slate-700">
          <h3 className="font-medium text-text-primary dark:text-slate-100 mb-3">Key Implementation Details</h3>
          <ul className="space-y-2 text-sm text-text-secondary dark:text-slate-400">
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>Fixed button height</strong> (<code className="px-1 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs">h-8</code>) prevents vertical layout shift when badge appears</span>
            </li>
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>Mode selector always rendered</strong> with opacity transition (not conditional) to prevent layout shift in panel</span>
            </li>
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>Reserved space for descriptions</strong> (<code className="px-1 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs">h-4</code>) prevents shift when mode changes</span>
            </li>
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>Panel uses fixed positioning</strong> with <code className="px-1 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs">z-[100]</code> and explicit coordinates for full viewport coverage</span>
            </li>
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>300ms transitions</strong> with <code className="px-1 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-xs">ease-out</code> for smooth panel animation</span>
            </li>
            <li className="flex items-start gap-2">
              <Check className="w-4 h-4 text-green-500 mt-0.5 flex-shrink-0" />
              <span><strong>Escape key closes panel</strong> for keyboard accessibility</span>
            </li>
          </ul>
        </div>
      </div>

      {/* Production Checklist */}
      <div className="space-y-4">
        <h2 className="text-xl font-semibold text-text-primary dark:text-slate-100">
          Production Implementation Checklist
        </h2>
        <div className="p-4 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20">
          <ul className="space-y-2 text-sm text-amber-800 dark:text-amber-300">
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Extract reusable components: <code>FilterChip</code>, <code>MultiSelectFilter</code>, <code>RangeFilter</code>, <code>FilterPanel</code></span>
            </li>
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Integrate with existing filter state management in <code>/spectra</code> page</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Update <code>get_spectra_filtered</code> RPC to support filter mode (any/all/none) parameters</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Add exposure time column to database schema for spectra-specific filtering</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Test on various screen sizes (1280px, 1440px, 1920px)</span>
            </li>
            <li className="flex items-start gap-2">
              <span className="w-5 h-5 rounded border border-amber-400 dark:border-amber-600 flex-shrink-0" />
              <span>Verify keyboard navigation and screen reader accessibility</span>
            </li>
          </ul>
        </div>
      </div>

      {/* Files Reference */}
      <div className="p-4 rounded-lg bg-slate-50 dark:bg-slate-800 border border-border dark:border-slate-700">
        <h3 className="font-medium text-text-primary dark:text-slate-100 mb-2">Prototype Files</h3>
        <ul className="text-sm text-text-secondary dark:text-slate-400 space-y-1 font-mono">
          <li>web/app/prototype/overflow-panel/page.tsx - Complete working prototype</li>
          <li>web/lib/mocks/spectra-mock-data.ts - Mock data and filter logic</li>
          <li>web/components/ui/RangeFilterChip.tsx - Reusable range filter component</li>
        </ul>
      </div>
    </div>
  );
}
