'use client';

import React, { useState } from 'react';
import { Layers, MapPin, Grid3X3, ChevronDown, ChevronUp, SlidersHorizontal } from 'lucide-react';
import type { MapLayer } from '@/lib/actions/map';

interface LayerControlProps {
  fields: string[];
  selectedField: string;
  onFieldChange: (field: string) => void;
  layers: MapLayer[];
  activeLayer: MapLayer | null;
  onLayerChange: (layer: MapLayer) => void;
  showMarkers: boolean;
  onToggleMarkers: (show: boolean) => void;
  markerCount: number;
  isLoadingMarkers: boolean;
  filteredMarkerCount?: number;
  showSlits: boolean;
  onToggleSlits: (show: boolean) => void;
  slitCount: number;
  isLoadingSlits?: boolean;
  filteredSlitCount?: number;
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
}

export function LayerControl({
  fields,
  selectedField,
  onFieldChange,
  layers,
  activeLayer,
  onLayerChange,
  showMarkers,
  onToggleMarkers,
  markerCount,
  isLoadingMarkers,
  filteredMarkerCount,
  showSlits,
  onToggleSlits,
  slitCount,
  isLoadingSlits,
  filteredSlitCount,
  onOpenFilters,
  hasActiveFilters,
}: LayerControlProps) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="absolute top-[80px] left-2.5 z-[1000] bg-white dark:bg-slate-800 rounded-lg shadow-lg border border-gray-200 dark:border-slate-700 min-w-[180px]">
      {/* Header — always visible, click to collapse */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-gray-500 dark:text-slate-400 uppercase tracking-wide hover:bg-gray-50 dark:hover:bg-slate-700/50 rounded-t-lg transition-colors"
      >
        <span className="flex items-center gap-1.5">
          <Layers className="w-3.5 h-3.5" />
          Layers
        </span>
        {collapsed ? (
          <ChevronDown className="w-3.5 h-3.5" />
        ) : (
          <ChevronUp className="w-3.5 h-3.5" />
        )}
      </button>

      {!collapsed && (
        <div className="px-3 pb-3">
          {/* Field selector */}
          {fields.length > 1 && (
            <div className="mb-3">
              <label className="block text-xs font-medium text-gray-500 dark:text-slate-400 mb-1 uppercase tracking-wide">
                Field
              </label>
              <div className="relative">
                <select
                  value={selectedField}
                  onChange={(e) => onFieldChange(e.target.value)}
                  className="w-full appearance-none bg-gray-50 dark:bg-slate-700 border border-gray-200 dark:border-slate-600 rounded px-3 py-1.5 text-sm text-gray-900 dark:text-slate-100 pr-8 uppercase"
                >
                  {fields.map((f) => (
                    <option key={f} value={f}>
                      {f.toUpperCase()}
                    </option>
                  ))}
                </select>
                <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
              </div>
            </div>
          )}

          {/* Filter/layer selector — 3-column grid */}
          <div className="mb-3">
            <label className="block text-xs font-medium text-gray-500 dark:text-slate-400 mb-1.5 uppercase tracking-wide">
              Band
            </label>
            <div className="grid grid-cols-3 gap-1">
              {layers.map((layer) => (
                <button
                  key={layer.id}
                  onClick={() => onLayerChange(layer)}
                  className={`px-1.5 py-1 rounded text-xs text-center transition-colors ${
                    layer.filter === 'rgb' ? 'col-span-3' : ''
                  } ${
                    activeLayer?.id === layer.id
                      ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-300 font-medium'
                      : 'hover:bg-gray-100 dark:hover:bg-slate-700 text-gray-700 dark:text-slate-300'
                  }`}
                >
                  {layer.filter.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          {/* Marker toggle */}
          <div className="pt-2 border-t border-gray-200 dark:border-slate-700">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showMarkers}
                onChange={(e) => onToggleMarkers(e.target.checked)}
                className="rounded border-gray-300 dark:border-slate-600"
              />
              <MapPin className="w-3.5 h-3.5 text-gray-500 dark:text-slate-400" />
              <span className="text-sm text-gray-700 dark:text-slate-300">
                Objects
                {isLoadingMarkers ? (
                  <span className="text-xs text-gray-400 ml-1">loading...</span>
                ) : filteredMarkerCount !== undefined && markerCount > 0 ? (
                  <span className="text-xs text-gray-400 ml-1">({filteredMarkerCount} of {markerCount})</span>
                ) : markerCount > 0 ? (
                  <span className="text-xs text-gray-400 ml-1">({markerCount})</span>
                ) : null}
              </span>
            </label>
          </div>

          {/* Shutter toggle */}
          <div className="mt-1.5">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={showSlits}
                onChange={(e) => onToggleSlits(e.target.checked)}
                disabled={isLoadingSlits || slitCount === 0}
                className="rounded border-gray-300 dark:border-slate-600 disabled:opacity-50"
              />
              <Grid3X3 className="w-3.5 h-3.5 text-gray-500 dark:text-slate-400" />
              <span className="text-sm text-gray-700 dark:text-slate-300">
                Shutters
                {isLoadingSlits ? (
                  <span className="text-xs text-gray-400 ml-1">loading...</span>
                ) : filteredSlitCount !== undefined && slitCount > 0 ? (
                  <span className="text-xs text-gray-400 ml-1">({filteredSlitCount} of {slitCount})</span>
                ) : slitCount > 0 ? (
                  <span className="text-xs text-gray-400 ml-1">({slitCount})</span>
                ) : null}
              </span>
            </label>
          </div>

          {/* Filter button */}
          {onOpenFilters && (
            <div className="pt-2 border-t border-gray-200 dark:border-slate-700 mt-2">
              <button
                onClick={onOpenFilters}
                className={`
                  w-full flex items-center gap-2 px-2.5 py-1.5 rounded text-sm transition-colors
                  ${hasActiveFilters
                    ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-300 font-medium'
                    : 'hover:bg-gray-100 dark:hover:bg-slate-700 text-gray-700 dark:text-slate-300'
                  }
                `}
              >
                <div className="relative">
                  <SlidersHorizontal className="w-3.5 h-3.5" />
                  {hasActiveFilters && (
                    <div className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-blue-500" />
                  )}
                </div>
                <span>Filters</span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
