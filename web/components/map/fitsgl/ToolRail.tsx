'use client';

/**
 * Left tool rail (epic #337, Phase 4.5) — a slim docked-left glass column
 * (`docs/design-fitsgl-map-ux.md` §4): modal cursor tools (pan / ruler) above a
 * divider, then one-shot actions (fit-to-view, export PNG) and the Filters launcher
 * (opens the shared slide-over). The Display/Layers panels live in the right dock
 * (its own collapse chevron), so they're not duplicated here.
 */

import { Hand, Ruler, Maximize2, Download, SlidersHorizontal } from 'lucide-react';
import { GLASS } from './glass';

type Tool = 'pan' | 'ruler';

interface ToolButtonProps {
  label: string;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
  /** A small accent dot (e.g. active-filters indicator). */
  dot?: boolean;
}

function ToolButton({ label, active, onClick, children, dot }: ToolButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={active}
      className={`relative flex h-9 w-9 items-center justify-center rounded-lg transition-colors ${active ? 'bg-primary-soft text-primary-text' : 'text-text-secondary hover:bg-card-hover hover:text-text-primary'}`}
    >
      {children}
      {dot && <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-primary" />}
    </button>
  );
}

interface ToolRailProps {
  tool: Tool;
  onSetTool: (tool: Tool) => void;
  onFit: () => void;
  onExport: () => void;
  onOpenFilters?: () => void;
  hasActiveFilters?: boolean;
}

export function ToolRail({ tool, onSetTool, onFit, onExport, onOpenFilters, hasActiveFilters }: ToolRailProps) {
  return (
    <div className={`absolute left-0 top-1/2 z-[500] flex -translate-y-1/2 flex-col gap-1 rounded-r-xl ${GLASS} p-1.5`}>
      <ToolButton label="Pan" active={tool === 'pan'} onClick={() => onSetTool('pan')}>
        <Hand className="h-4 w-4" />
      </ToolButton>
      <ToolButton label="Ruler (measure)" active={tool === 'ruler'} onClick={() => onSetTool('ruler')}>
        <Ruler className="h-4 w-4" />
      </ToolButton>

      <div className="mx-auto my-0.5 h-px w-5 bg-border" />

      <ToolButton label="Fit to view" onClick={onFit}>
        <Maximize2 className="h-4 w-4" />
      </ToolButton>
      <ToolButton label="Export PNG" onClick={onExport}>
        <Download className="h-4 w-4" />
      </ToolButton>
      {onOpenFilters && (
        <ToolButton label="NIRSpec filters" onClick={onOpenFilters} dot={hasActiveFilters}>
          <SlidersHorizontal className="h-4 w-4" />
        </ToolButton>
      )}
    </div>
  );
}
