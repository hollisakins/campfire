'use client';

import React, { useState, useEffect, useCallback, useTransition, useRef } from 'react';
import { Plus, X, Loader2, Check, AlertCircle } from 'lucide-react';
import { getListsWithMembership, addObjectToList, removeObjectFromList } from '@/lib/actions/lists';
import { useAuth } from '@/lib/contexts/AuthContext';
import { getContrastColor } from '@/lib/flags';
import type { ObjectListWithMembership } from '@/lib/types';

interface ObjectListsSectionProps {
  objectId: number;
  ra: number;
  dec: number;
}

export function ObjectListsSection({ objectId, ra, dec }: ObjectListsSectionProps) {
  const { userProfile } = useAuth();
  const canEdit = !!userProfile?.can_comment;

  const [lists, setLists] = useState<ObjectListWithMembership[]>([]);
  const [loading, setLoading] = useState(true);
  const [showDropdown, setShowDropdown] = useState(false);
  const [isPending, startTransition] = useTransition();
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getListsWithMembership(objectId).then(({ lists: data }) => {
      setLists(data);
      setLoading(false);
    });
  }, [objectId]);

  // Close dropdown on outside click
  useEffect(() => {
    if (!showDropdown) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showDropdown]);

  // Auto-dismiss toast
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(timer);
  }, [toast]);

  const memberLists = lists.filter(l => l.is_member);
  const availableLists = lists.filter(l => !l.is_member);

  const handleToggle = useCallback((list: ObjectListWithMembership) => {
    startTransition(async () => {
      if (list.is_member) {
        const { error } = await removeObjectFromList(list.id, objectId);
        if (error) {
          setToast({ type: 'error', message: `Failed to remove from ${list.name}` });
        } else {
          setLists(prev => prev.map(l => l.id === list.id ? { ...l, is_member: false } : l));
          setToast({ type: 'success', message: `Removed from ${list.name}` });
        }
      } else {
        const { error } = await addObjectToList(list.id, objectId, ra, dec);
        if (error) {
          setToast({ type: 'error', message: `Failed to add to ${list.name}` });
        } else {
          setLists(prev => prev.map(l => l.id === list.id ? { ...l, is_member: true } : l));
          setShowDropdown(false);
          setToast({ type: 'success', message: `Added to ${list.name}` });
        }
      }
    });
  }, [objectId, ra, dec]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400">
        <Loader2 className="w-3 h-3 animate-spin" />
        Loading lists...
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {memberLists.map(list => (
          <span
            key={list.id}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium"
            style={{
              backgroundColor: list.color ?? '#e0e0e0',
              color: getContrastColor(list.color ?? '#e0e0e0'),
            }}
          >
            {list.icon && <span>{list.icon}</span>}
            {list.name}
            {canEdit && (
              <button
                onClick={() => handleToggle(list)}
                disabled={isPending}
                className="ml-0.5 hover:opacity-70 transition-opacity"
                title={`Remove from ${list.name}`}
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </span>
        ))}

        {canEdit && (
          <div className="relative" ref={dropdownRef}>
            <button
              onClick={() => setShowDropdown(!showDropdown)}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium border border-dashed border-gray-300 dark:border-slate-600 text-text-secondary dark:text-slate-400 hover:border-primary hover:text-primary transition-colors"
            >
              <Plus className="w-3 h-3" />
              Add to list
            </button>

            {showDropdown && (
              <div className="absolute top-full left-0 mt-1 z-50 w-56 bg-white dark:bg-slate-800 rounded-lg shadow-lg border border-gray-200 dark:border-slate-700 py-1 max-h-64 overflow-y-auto">
                {availableLists.length === 0 ? (
                  <div className="px-3 py-2 text-xs text-text-secondary dark:text-slate-400">
                    Already in all available lists
                  </div>
                ) : (
                  availableLists.map(list => (
                    <button
                      key={list.id}
                      onClick={() => handleToggle(list)}
                      disabled={isPending}
                      className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-slate-700 flex items-center gap-2"
                    >
                      {list.icon && <span>{list.icon}</span>}
                      <span className="flex-1">{list.name}</span>
                      {list.color && (
                        <span
                          className="w-3 h-3 rounded-full"
                          style={{ backgroundColor: list.color }}
                        />
                      )}
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
        )}

        {memberLists.length === 0 && !canEdit && (
          <span className="text-xs text-text-secondary dark:text-slate-400">
            No list memberships
          </span>
        )}
      </div>

      {toast && (
        <div className={`inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded ${
          toast.type === 'success'
            ? 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20'
            : 'text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20'
        }`}>
          {toast.type === 'success' ? <Check className="w-3 h-3" /> : <AlertCircle className="w-3 h-3" />}
          {toast.message}
        </div>
      )}
    </div>
  );
}
