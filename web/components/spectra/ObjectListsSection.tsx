'use client';

import React, { useState, useEffect, useCallback, useTransition, useRef } from 'react';
import { Plus, X, Loader2, Check, AlertCircle, Search, Tag } from 'lucide-react';
import { getListsWithMembership, addObjectToList, removeObjectFromList } from '@/lib/actions/lists';
import { ListForm } from '@/components/lists/ListForm';
import { useAuth } from '@/lib/contexts/AuthContext';
import { getContrastColor } from '@/lib/flags';
import type { ObjectList, ObjectListWithMembership } from '@/lib/types';

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
  const availableLists = lists.filter(l => !l.is_member);

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

  const handleTagCreated = useCallback(async (newList: ObjectList) => {
    // Auto-add the object to the newly created tag
    const { error } = await addObjectToList(newList.id, objectId, ra, dec);
    if (error) {
      setToast({ type: 'error', message: error });
      return;
    }

    setLists(prev => [...prev, { ...newList, is_member: true } as ObjectListWithMembership]);
    setToast({ type: 'success', message: `Created and tagged with ${newList.name}` });
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
                onClick={() => handleRemove(list)}
                disabled={isPending}
                className="ml-0.5 hover:opacity-70 transition-opacity"
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
              className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium border border-dashed border-gray-300 dark:border-slate-600 text-text-secondary dark:text-slate-400 hover:border-primary hover:text-primary transition-colors"
            >
              <Plus className="w-3 h-3" />
              Add tag
            </button>

            {showDropdown && (
              <div className="absolute top-full left-0 mt-1 z-50 w-64 bg-white dark:bg-slate-800 rounded-lg shadow-lg border border-gray-200 dark:border-slate-700">
                {/* Search input */}
                <div className="p-2 border-b border-gray-200 dark:border-slate-700">
                  <div className="relative">
                    <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-secondary dark:text-slate-500" />
                    <input
                      ref={searchInputRef}
                      type="text"
                      value={searchTerm}
                      onChange={(e) => setSearchTerm(e.target.value)}
                      onKeyDown={handleKeyDown}
                      placeholder="Search or create tag..."
                      className="w-full pl-8 pr-3 py-1.5 text-sm border border-gray-200 dark:border-slate-700 rounded-md bg-white dark:bg-slate-900 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-primary focus:border-transparent"
                    />
                  </div>
                </div>

                {/* Tag options */}
                <div className="max-h-[240px] overflow-y-auto p-1">
                  {sortedAvailable.length > 0 ? (
                    sortedAvailable.map(list => (
                      <button
                        key={list.id}
                        onClick={() => handleAdd(list)}
                        disabled={isPending}
                        className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-slate-700 rounded-md flex items-center gap-2 transition-colors"
                      >
                        {list.color && (
                          <span
                            className="w-3 h-3 rounded-full flex-shrink-0"
                            style={{ backgroundColor: list.color }}
                          />
                        )}
                        {list.icon && <span className="text-sm">{list.icon}</span>}
                        <span className="flex-1 truncate">{list.name}</span>
                        <span className="text-[10px] text-text-secondary dark:text-slate-500 flex-shrink-0">
                          #{list.slug}
                        </span>
                      </button>
                    ))
                  ) : !showCreateOption ? (
                    <div className="px-3 py-4 text-xs text-text-secondary dark:text-slate-400 text-center">
                      {normalizedSearch ? 'No matching tags' : 'All tags applied'}
                    </div>
                  ) : null}

                  {/* Create new tag option */}
                  {showCreateOption && (
                    <button
                      onClick={handleOpenCreateModal}
                      className="w-full text-left px-3 py-2 text-sm hover:bg-primary/10 rounded-md flex items-center gap-2 transition-colors text-primary border-t border-gray-100 dark:border-slate-700 mt-1 pt-2"
                    >
                      <Tag className="w-3.5 h-3.5 flex-shrink-0" />
                      <span>Create <strong>{searchTerm.trim()}</strong></span>
                    </button>
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
