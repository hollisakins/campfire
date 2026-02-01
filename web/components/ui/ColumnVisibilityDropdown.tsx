'use client';

import React, { useState, useRef, useEffect } from 'react';
import { Columns3, ChevronDown, RotateCcw, Check, Eye } from 'lucide-react';

export interface ColumnDefinition {
  id: string;
  label: string;
  defaultVisible?: boolean;
  alwaysVisible?: boolean;  // Cannot be hidden (e.g., object_id)
}

interface ColumnVisibilityDropdownProps {
  columns: ColumnDefinition[];
  visibility: Record<string, boolean>;
  onChange: (visibility: Record<string, boolean>) => void;
  className?: string;
}

export const ColumnVisibilityDropdown: React.FC<ColumnVisibilityDropdownProps> = ({
  columns,
  visibility,
  onChange,
  className = '',
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const toggleColumn = (columnId: string) => {
    const column = columns.find(c => c.id === columnId);
    if (column?.alwaysVisible) return;

    onChange({
      ...visibility,
      [columnId]: !visibility[columnId],
    });
  };

  const resetToDefaults = () => {
    const defaults: Record<string, boolean> = {};
    columns.forEach(col => {
      defaults[col.id] = col.alwaysVisible || col.defaultVisible !== false;
    });
    onChange(defaults);
  };

  const visibleCount = Object.values(visibility).filter(Boolean).length;

  const showAll = () => {
    const newVisibility: Record<string, boolean> = {};
    columns.forEach(col => {
      newVisibility[col.id] = true;
    });
    onChange(newVisibility);
  };

  return (
    <div ref={containerRef} className={`relative inline-block ${className}`}>
      {/* Trigger Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={`
          inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium
          border transition-all duration-150
          bg-card dark:bg-slate-800 border-border dark:border-slate-700
          text-text-secondary dark:text-slate-400
          hover:border-text-secondary dark:hover:border-slate-600
          hover:text-text-primary dark:hover:text-slate-200
        `}
      >
        <Columns3 className="w-4 h-4" />
        <span>Columns</span>
        <span className="text-xs px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400">
          {visibleCount}
        </span>
        <ChevronDown className={`w-3.5 h-3.5 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute right-0 z-50 mt-2 w-64 max-h-[400px] overflow-y-auto bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg">
          {/* Header with quick actions */}
          <div className="sticky top-0 z-10 bg-background dark:bg-slate-800 border-b border-border dark:border-slate-700 p-2 flex gap-2">
            <button
              onClick={showAll}
              className="flex-1 flex items-center justify-center gap-1 px-2 py-1 text-xs rounded bg-slate-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors"
            >
              <Eye className="w-3 h-3" />
              Show all
            </button>
            <button
              onClick={resetToDefaults}
              className="flex-1 flex items-center justify-center gap-1 px-2 py-1 text-xs rounded bg-slate-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors"
            >
              <RotateCcw className="w-3 h-3" />
              Defaults
            </button>
          </div>

          {/* Column list */}
          <div className="p-2 space-y-1">
            {columns.map((column) => {
              const isVisible = visibility[column.id] ?? true;
              const isLocked = column.alwaysVisible;

              return (
                <button
                  key={column.id}
                  onClick={() => toggleColumn(column.id)}
                  disabled={isLocked}
                  className={`
                    w-full flex items-center gap-3 px-3 py-2 rounded-md text-sm text-left
                    transition-colors
                    ${isLocked
                      ? 'opacity-60 cursor-not-allowed bg-slate-50 dark:bg-slate-800'
                      : isVisible
                        ? 'bg-primary/10 dark:bg-primary/20 text-text-primary dark:text-slate-100'
                        : 'text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700'
                    }
                  `}
                >
                  {/* Checkbox indicator */}
                  <div className={`
                    w-4 h-4 rounded border flex items-center justify-center flex-shrink-0
                    ${isVisible
                      ? 'bg-primary border-primary'
                      : 'border-border dark:border-slate-600'
                    }
                  `}>
                    {isVisible && <Check className="w-3 h-3 text-white" />}
                  </div>

                  {/* Label */}
                  <span className="flex-1">{column.label}</span>

                  {/* Lock indicator */}
                  {isLocked && (
                    <span className="text-xs text-text-secondary dark:text-slate-500">
                      required
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
};

// Helper to get default visibility state
export function getDefaultColumnVisibility(columns: ColumnDefinition[]): Record<string, boolean> {
  const visibility: Record<string, boolean> = {};
  columns.forEach(col => {
    visibility[col.id] = col.alwaysVisible || col.defaultVisible !== false;
  });
  return visibility;
}

// Hook to manage column visibility with localStorage persistence
export function useColumnVisibility(
  columns: ColumnDefinition[],
  storageKey: string = 'campfire-column-visibility'
): [Record<string, boolean>, (visibility: Record<string, boolean>) => void] {
  const [visibility, setVisibility] = useState<Record<string, boolean>>(() => {
    // SSR-safe: return defaults on server
    if (typeof window === 'undefined') {
      return getDefaultColumnVisibility(columns);
    }

    // Try to load from localStorage
    try {
      const stored = localStorage.getItem(storageKey);
      if (stored) {
        const parsed = JSON.parse(stored);
        // Merge with defaults to handle new columns
        const defaults = getDefaultColumnVisibility(columns);
        return { ...defaults, ...parsed };
      }
    } catch {}

    return getDefaultColumnVisibility(columns);
  });

  // Save to localStorage on change
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(visibility));
    } catch {}
  }, [visibility, storageKey]);

  return [visibility, setVisibility];
}
