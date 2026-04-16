'use client';

import React, { useState, useRef, useCallback } from 'react';
import { GripVertical } from 'lucide-react';
import type { ObjectMemberTarget } from '@/lib/types';
import { GRATINGS } from '@/lib/types';

interface ObjectSidebarProps {
  members: ObjectMemberTarget[];
  colors: Record<string, string>;
  visibility: Record<string, boolean>;
  onVisibilityChange: (targetId: string, visible: boolean) => void;
  onToggleAll: (visible: boolean) => void;
  onReorder: (orderedIds: string[]) => void;
}

export const ObjectSidebar: React.FC<ObjectSidebarProps> = ({
  members,
  colors,
  visibility,
  onVisibilityChange,
  onToggleAll,
  onReorder,
}) => {
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

  const handleContainerDragLeave = useCallback((e: React.DragEvent) => {
    if (containerRef.current && !containerRef.current.contains(e.relatedTarget as Node)) {
      setDropTargetId(null);
    }
  }, []);

  return (
    <nav>
      <div className="flex items-center gap-2 mb-2 px-1">
        <input
          type="checkbox"
          checked={allChecked}
          ref={(el) => { if (el) el.indeterminate = !allChecked && !noneChecked; }}
          onChange={() => onToggleAll(!allChecked)}
          title={allChecked ? 'Hide all from plot' : 'Show all in plot'}
          className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent w-3.5 h-3.5"
        />
        <span className="text-xs font-medium uppercase tracking-wide text-text-secondary dark:text-slate-500">
          Members ({members.length})
        </span>
      </div>

      <div
        ref={containerRef}
        className="space-y-0.5"
        onDragLeave={handleContainerDragLeave}
      >
        {members.map((member) => {
          const memberGratings = [...new Set(member.spectra.map(s => s.grating))];
          const sortedGratings = GRATINGS.filter(g => memberGratings.includes(g));
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
              className={`flex items-start gap-1.5 rounded-lg text-sm transition-all border-l-3 border-l-transparent hover:bg-gray-100 dark:hover:bg-slate-800 ${
                isDragged ? 'opacity-30' : ''
              } ${
                isDropTarget && dropPosition === 'above' ? 'border-t-2 border-t-accent' : ''
              } ${
                isDropTarget && dropPosition === 'below' ? 'border-b-2 border-b-accent' : ''
              }`}
            >
              <div className="pt-2.5 pl-1 cursor-grab active:cursor-grabbing">
                <GripVertical className="w-3.5 h-3.5 text-text-secondary/40 dark:text-slate-600" />
              </div>

              <div className="pt-2.5">
                <input
                  type="checkbox"
                  checked={visibility[member.target_id] ?? true}
                  onChange={(e) => onVisibilityChange(member.target_id, e.target.checked)}
                  className="rounded border-gray-300 dark:border-slate-600 text-accent focus:ring-accent w-3.5 h-3.5"
                />
              </div>

              <div className="flex-1 py-2 pr-2 min-w-0">
                <div className="flex items-center gap-1.5 mb-0.5">
                  <div
                    className="w-2.5 h-2.5 rounded-full flex-shrink-0"
                    style={{ backgroundColor: colors[member.target_id] }}
                  />
                  <span
                    className="text-xs font-mono truncate text-text-primary dark:text-slate-200"
                    title={member.target_id}
                  >
                    {member.target_id}
                  </span>
                </div>
                <div className="pl-4 text-xs text-text-secondary dark:text-slate-500">
                  <div className="truncate">{member.program_name}</div>
                  <div className="flex items-center gap-1 mt-0.5 flex-wrap">
                    {sortedGratings.map(g => (
                      <span
                        key={g}
                        className="text-[10px] px-1 py-0.5 rounded font-mono bg-gray-100 dark:bg-slate-700 text-text-secondary dark:text-slate-400"
                      >
                        {g}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </nav>
  );
};
