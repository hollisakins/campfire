'use client';

import React, { useState, useRef, useCallback } from 'react';
import { LayoutGrid, GripVertical } from 'lucide-react';
import type { ObjectMemberTarget } from '@/lib/types';
import { QUALITY_LABELS, GRATINGS } from '@/lib/types';

interface ObjectSidebarProps {
  members: ObjectMemberTarget[];
  activeTab: string; // 'overview' | target_id
  onTabChange: (tab: string) => void;
  colors: Record<string, string>;
  visibility: Record<string, boolean>;
  onVisibilityChange: (targetId: string, visible: boolean) => void;
  onToggleAll: (visible: boolean) => void;
  onReorder: (orderedIds: string[]) => void;
}

export const ObjectSidebar: React.FC<ObjectSidebarProps> = ({
  members,
  activeTab,
  onTabChange,
  colors,
  visibility,
  onVisibilityChange,
  onToggleAll,
  onReorder,
}) => {
  // Drag state
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [dropPosition, setDropPosition] = useState<'above' | 'below'>('below');
  const containerRef = useRef<HTMLDivElement>(null);

  const allChecked = members.every(m => visibility[m.target_id]);
  const noneChecked = members.every(m => !visibility[m.target_id]);

  const handleDragStart = useCallback((e: React.DragEvent, targetId: string) => {
    setDraggedId(targetId);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', targetId);
  }, []);

  // dragover fires continuously on the hovered element — reliable source of truth
  const handleDragOver = useCallback((e: React.DragEvent, targetId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (targetId === draggedId) {
      setDropTargetId(null);
      return;
    }

    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    setDropTargetId(targetId);
    setDropPosition(e.clientY < midY ? 'above' : 'below');
  }, [draggedId]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (!draggedId || !dropTargetId || draggedId === dropTargetId) return;

    const ids = members.map(m => m.target_id);
    const fromIndex = ids.indexOf(draggedId);
    if (fromIndex === -1) return;

    ids.splice(fromIndex, 1);
    let toIndex = ids.indexOf(dropTargetId);
    if (toIndex === -1) return;
    if (dropPosition === 'below') toIndex++;
    ids.splice(toIndex, 0, draggedId);

    onReorder(ids);
  }, [draggedId, dropTargetId, dropPosition, members, onReorder]);

  const handleDragEnd = useCallback(() => {
    setDraggedId(null);
    setDropTargetId(null);
  }, []);

  // Clear drop indicator when pointer leaves the member list container entirely
  const handleContainerDragLeave = useCallback((e: React.DragEvent) => {
    if (containerRef.current && !containerRef.current.contains(e.relatedTarget as Node)) {
      setDropTargetId(null);
    }
  }, []);

  return (
    <nav className="w-60 flex-shrink-0 border-r border-border dark:border-slate-700 pr-3 sticky top-4 max-h-[calc(100vh-6rem)] overflow-y-auto">
      {/* Overview tab */}
      <button
        onClick={() => onTabChange('overview')}
        className={`w-full text-left px-3 py-2.5 rounded-lg text-sm font-medium transition-colors flex items-center gap-2 ${
          activeTab === 'overview'
            ? 'bg-accent/10 text-accent dark:bg-accent/20'
            : 'text-text-secondary dark:text-slate-400 hover:bg-gray-100 dark:hover:bg-slate-800 hover:text-text-primary dark:hover:text-slate-200'
        }`}
      >
        <LayoutGrid className="w-4 h-4 flex-shrink-0" />
        Overview
      </button>

      {/* Divider with toggle-all checkbox */}
      <div className="flex items-center gap-2 my-2 px-1">
        <div className="border-t border-border dark:border-slate-700 flex-1" />
        <input
          type="checkbox"
          checked={allChecked}
          ref={(el) => { if (el) el.indeterminate = !allChecked && !noneChecked; }}
          onChange={() => onToggleAll(!allChecked)}
          title={allChecked ? 'Hide all from plot' : 'Show all in plot'}
          className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent w-3.5 h-3.5"
        />
        <div className="border-t border-border dark:border-slate-700 flex-1" />
      </div>

      {/* Member target tabs */}
      <div
        ref={containerRef}
        className="space-y-0.5"
        onDragLeave={handleContainerDragLeave}
      >
        {members.map((member) => {
          const qualityDef = QUALITY_LABELS.find(q => q.value === member.redshift_quality);
          const memberGratings = [...new Set(member.spectra.map(s => s.grating))];
          const sortedGratings = GRATINGS.filter(g => memberGratings.includes(g));
          const isActive = activeTab === member.target_id;
          const isDragged = draggedId === member.target_id;
          const isDropTarget = dropTargetId === member.target_id && draggedId !== member.target_id;

          return (
            <div
              key={member.target_id}
              draggable
              onDragStart={(e) => handleDragStart(e, member.target_id)}
              onDragOver={(e) => handleDragOver(e, member.target_id)}
              onDrop={handleDrop}
              onDragEnd={handleDragEnd}
              className={`flex items-start gap-1.5 rounded-lg text-sm transition-all ${
                isActive
                  ? 'bg-accent/10 dark:bg-accent/20 border-l-3 border-l-accent'
                  : 'hover:bg-gray-100 dark:hover:bg-slate-800 border-l-3 border-l-transparent'
              } ${isDragged ? 'opacity-30' : ''} ${
                isDropTarget && dropPosition === 'above' ? 'border-t-2 border-t-accent' : ''
              } ${
                isDropTarget && dropPosition === 'below' ? 'border-b-2 border-b-accent' : ''
              }`}
            >
              {/* Drag handle */}
              <div className="pt-2.5 pl-1 cursor-grab active:cursor-grabbing">
                <GripVertical className="w-3.5 h-3.5 text-text-secondary/40 dark:text-slate-600" />
              </div>

              {/* Visibility checkbox */}
              <div className="pt-2.5">
                <input
                  type="checkbox"
                  checked={visibility[member.target_id] ?? true}
                  onChange={(e) => {
                    e.stopPropagation();
                    onVisibilityChange(member.target_id, e.target.checked);
                  }}
                  className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent w-3.5 h-3.5"
                />
              </div>

              {/* Clickable target info */}
              <button
                onClick={() => onTabChange(member.target_id)}
                className="flex-1 text-left py-2 pr-2 min-w-0"
              >
                <div className="flex items-center gap-1.5 mb-0.5">
                  <div
                    className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                    style={{ backgroundColor: colors[member.target_id] }}
                  />
                  <span
                    className={`font-mono truncate ${
                      isActive
                        ? 'text-sm text-accent font-semibold'
                        : 'text-xs text-text-primary dark:text-slate-200'
                    }`}
                    title={member.target_id}
                  >
                    {member.target_id}
                  </span>
                  {qualityDef && (
                    <span
                      className="w-2 h-2 rounded-full flex-shrink-0 ml-auto"
                      style={{ backgroundColor: qualityDef.color ?? undefined }}
                      title={qualityDef.label}
                    />
                  )}
                </div>
                <div className="pl-4 text-xs text-text-secondary dark:text-slate-500">
                  <div className="flex items-center gap-1">
                    <span className="truncate">{member.program_name}</span>
                  </div>
                  <div className="flex items-center gap-1 mt-0.5">
                    {member.redshift != null && (
                      <span className="font-mono">z={member.redshift.toFixed(3)}</span>
                    )}
                    <span className="ml-auto flex-shrink-0 flex gap-0.5">
                      {sortedGratings.map(g => (
                        <span
                          key={g}
                          className="text-[11px] px-1 py-0.5 rounded font-mono bg-gray-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400"
                        >
                          {g}
                        </span>
                      ))}
                    </span>
                  </div>
                </div>
              </button>
            </div>
          );
        })}
      </div>
    </nav>
  );
};
