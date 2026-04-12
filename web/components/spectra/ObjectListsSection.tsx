'use client';

import React, { useState, useEffect, useCallback, useTransition, useRef } from 'react';
import { Plus, X, Loader2, Check, AlertCircle, Search, Tag } from 'lucide-react';
import { getListsWithMembership, addObjectToList, removeObjectFromList } from '@/lib/actions/lists';
import { ListForm } from '@/components/lists/ListForm';
import { useAuth } from '@/lib/contexts/AuthContext';
import { getContrastColor } from '@/lib/flags';
import type { ObjectList, ObjectListWithMembership } from '@/lib/types';

// Darken and saturate a hex color (same utility used in ChipSelect, FilterChip, etc.)
function darkenColor(hex: string, percent: number): string {
  const color = hex.replace('#', '');
  const num = parseInt(color, 16);
  const r = ((num >> 16) & 0xff) / 255;
  const g = ((num >> 8) & 0xff) / 255;
  const b = (num & 0xff) / 255;
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

interface ObjectListsSectionProps {
  objectId: number;
  ra: number;
  dec: number;
  /** Direction dropdown opens. Default 'bottom'. Use 'top' when near bottom of viewport. */
  dropdownPlacement?: 'bottom' | 'top';
}

export function ObjectListsSection({ objectId, ra, dec, dropdownPlacement = 'bottom' }: ObjectListsSectionProps) {
  const { user, userProfile } = useAuth();
  const canEdit = !!userProfile?.can_comment;

  /** Check if the current user can edit a specific list (owner or public_edit). */
  const canEditList = useCallback((list: ObjectListWithMembership) => {
    if (!canEdit) return false;
    return list.created_by === user?.id || list.visibility === 'public_edit';
  }, [canEdit, user?.id]);

  const [lists, setLists] = useState<ObjectListWithMembership[]>([]);
  const [loading, setLoading] = useState(true);
  const [showDropdown, setShowDropdown] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createInitialName, setCreateInitialName] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [isPending, startTransition] = useTransition();
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

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

  // Auto-focus search when dropdown opens
  useEffect(() => {
    if (showDropdown && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [showDropdown]);

  // Reset search when dropdown closes
  useEffect(() => {
    if (!showDropdown) setSearchTerm('');
  }, [showDropdown]);

  // Auto-dismiss toast
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(timer);
  }, [toast]);

  const memberLists = lists.filter(l => l.is_member);
  const availableLists = lists.filter(l => !l.is_member && canEditList(l));

  // Filter available tags by search term (matches name or slug shortname)
  const normalizedSearch = searchTerm.trim().toLowerCase();
  const filteredAvailable = normalizedSearch
    ? availableLists.filter(l =>
        l.name.toLowerCase().includes(normalizedSearch) ||
        l.slug.includes(normalizedSearch)
      )
    : availableLists;

  // Sort: starts-with matches first (name or slug), then contains
  const sortedAvailable = normalizedSearch
    ? [...filteredAvailable].sort((a, b) => {
        const aStarts = (a.name.toLowerCase().startsWith(normalizedSearch) || a.slug.startsWith(normalizedSearch)) ? 0 : 1;
        const bStarts = (b.name.toLowerCase().startsWith(normalizedSearch) || b.slug.startsWith(normalizedSearch)) ? 0 : 1;
        return aStarts - bStarts || a.name.localeCompare(b.name);
      })
    : filteredAvailable;

  // Check if search term exactly matches an existing tag name or slug
  const exactMatch = normalizedSearch && lists.some(l =>
    l.name.toLowerCase() === normalizedSearch || l.slug === normalizedSearch
  );
  const showCreateOption = normalizedSearch.length >= 2 && !exactMatch;

  const handleAdd = useCallback((list: ObjectListWithMembership) => {
    startTransition(async () => {
      const { error } = await addObjectToList(list.id, objectId, ra, dec);
      if (error) {
        setToast({ type: 'error', message: `Failed to tag with ${list.name}` });
      } else {
        setLists(prev => prev.map(l => l.id === list.id ? { ...l, is_member: true } : l));
        setSearchTerm('');
        setToast({ type: 'success', message: `Tagged with ${list.name}` });
      }
    });
  }, [objectId, ra, dec]);

  const handleRemove = useCallback((list: ObjectListWithMembership) => {
    startTransition(async () => {
      const { error } = await removeObjectFromList(list.id, objectId);
      if (error) {
        setToast({ type: 'error', message: `Failed to remove tag ${list.name}` });
      } else {
        setLists(prev => prev.map(l => l.id === list.id ? { ...l, is_member: false } : l));
        setToast({ type: 'success', message: `Removed tag ${list.name}` });
      }
    });
  }, [objectId]);

  const handleOpenCreateModal = useCallback(() => {
    setCreateInitialName(searchTerm.trim());
    setShowDropdown(false);
    setShowCreateModal(true);
  }, [searchTerm]);

  const handleTagCreated = useCallback((newList: ObjectList) => {
    startTransition(async () => {
      // Auto-add the object to the newly created tag
      const { error } = await addObjectToList(newList.id, objectId, ra, dec);
      if (error) {
        setToast({ type: 'error', message: error });
        return;
      }

      setLists(prev => [...prev, { ...newList, is_member: true } as ObjectListWithMembership]);
      setToast({ type: 'success', message: `Created and tagged with ${newList.name}` });
    });
  }, [objectId, ra, dec]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setShowDropdown(false);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (sortedAvailable.length === 1) {
        handleAdd(sortedAvailable[0]);
      } else if (showCreateOption) {
        handleOpenCreateModal();
      }
    }
  }, [sortedAvailable, showCreateOption, handleAdd, handleOpenCreateModal]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-secondary dark:text-slate-400">
        <Loader2 className="w-3 h-3 animate-spin" />
        Loading tags...
      </div>
    );
  }

  return (
    <div className="relative">
      <div className="flex flex-wrap items-center gap-2">
        {memberLists.map(list => (
          <span
            key={list.id}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border"
            style={{
              backgroundColor: list.color ?? '#e0e0e0',
              color: getContrastColor(list.color ?? '#e0e0e0'),
              borderColor: darkenColor(list.color ?? '#e0e0e0', 20),
            }}
          >
            {list.icon && <span>{list.icon}</span>}
            {list.name}
            {canEditList(list) && (
              <button
                onClick={() => handleRemove(list)}
                disabled={isPending}
                className="ml-0.5 rounded-full p-0.5 hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
                title={`Remove tag ${list.name}`}
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
              className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium border border-dashed border-border dark:border-slate-600 text-text-secondary dark:text-slate-400 hover:border-primary hover:text-primary transition-colors"
            >
              <Plus className="w-3 h-3" />
              Add tag
            </button>

            {showDropdown && (
              <div className={`absolute ${dropdownPlacement === 'top' ? 'bottom-full mb-1' : 'top-full mt-1'} left-0 z-50 w-72 animate-zoom-in bg-background dark:bg-slate-800 rounded-lg shadow-lg border border-border dark:border-slate-700`}>
                {/* Search input */}
                <div className="p-2 border-b border-border dark:border-slate-700">
                  <div className="relative">
                    <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-secondary dark:text-slate-500" />
                    <input
                      ref={searchInputRef}
                      type="text"
                      value={searchTerm}
                      onChange={(e) => setSearchTerm(e.target.value)}
                      onKeyDown={handleKeyDown}
                      placeholder="Search or create tag..."
                      className="w-full pl-8 pr-3 py-1.5 text-sm border border-border dark:border-slate-700 rounded-md bg-background dark:bg-slate-900 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-primary focus:border-transparent"
                    />
                  </div>
                </div>

                {/* Tag options */}
                <div className="max-h-[240px] overflow-y-auto p-1.5">
                  {sortedAvailable.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {sortedAvailable.map(list => (
                        <button
                          key={list.id}
                          onClick={() => handleAdd(list)}
                          disabled={isPending}
                          className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border cursor-pointer transition-all duration-150 hover:brightness-90 hover:shadow-sm disabled:opacity-50"
                          style={{
                            backgroundColor: `${list.color ?? '#e0e0e0'}60`,
                            borderColor: darkenColor(list.color ?? '#e0e0e0', 20),
                            color: 'inherit',
                          }}
                        >
                          {list.icon && <span>{list.icon}</span>}
                          <span className="truncate">{list.name}</span>
                          <span className="opacity-50">#{list.slug}</span>
                        </button>
                      ))}
                    </div>
                  ) : !showCreateOption ? (
                    <div className="px-3 py-4 text-sm text-text-secondary dark:text-slate-500 text-center">
                      {normalizedSearch ? 'No matching tags' : 'All tags applied'}
                    </div>
                  ) : null}

                  {/* Create new tag option */}
                  {showCreateOption && (
                    <div className={sortedAvailable.length > 0 ? 'border-t border-border dark:border-slate-700 mt-1.5 pt-1.5' : ''}>
                      <button
                        onClick={handleOpenCreateModal}
                        className="w-full text-left px-3 py-2 text-sm hover:bg-primary/10 rounded-md flex items-center gap-2 transition-colors text-primary"
                      >
                        <Tag className="w-3.5 h-3.5 flex-shrink-0" />
                        <span>Create <strong>{searchTerm.trim()}</strong></span>
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {memberLists.length === 0 && !canEdit && (
          <span className="text-xs text-text-secondary dark:text-slate-400">
            No tags
          </span>
        )}
      </div>

      {/* Toast — absolutely positioned to avoid layout shift */}
      {toast && (
        <div className={`absolute left-0 ${dropdownPlacement === 'top' ? 'bottom-full mb-1.5' : 'top-full mt-1.5'} inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md animate-fade-in z-40 ${
          toast.type === 'success'
            ? 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800'
            : 'text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800'
        }`}>
          {toast.type === 'success' ? <Check className="w-3 h-3" /> : <AlertCircle className="w-3 h-3" />}
          {toast.message}
        </div>
      )}

      {/* Create tag modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={() => setShowCreateModal(false)}
          />
          <div className="relative w-full max-w-md mx-4">
            <ListForm
              mode="create"
              initialName={createInitialName}
              onCreated={handleTagCreated}
              onSuccess={() => setShowCreateModal(false)}
              onCancel={() => setShowCreateModal(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
