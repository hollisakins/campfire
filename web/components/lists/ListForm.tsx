'use client';

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Loader2, Check, X as XIcon } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { createList, updateList, checkSlugAvailability } from '@/lib/actions/lists';
import { ListEmojiPicker } from './ListEmojiPicker';
import { ListColorPicker } from './ListColorPicker';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { ObjectList } from '@/lib/types';

interface ListFormProps {
  mode: 'create' | 'edit';
  list?: ObjectList;
  initialName?: string;
  onSuccess: () => void;
  onCreated?: (list: ObjectList) => void;
  onCancel: () => void;
}

function nameToSlugFragment(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

export function ListForm({ mode, list, initialName, onSuccess, onCreated, onCancel }: ListFormProps) {
  const { userProfile } = useAuth();
  const username = userProfile?.username ?? '';

  const [name, setName] = useState(list?.name ?? initialName ?? '');
  const [slug, setSlug] = useState(list?.slug ?? '');
  const [slugTouched, setSlugTouched] = useState(mode === 'edit');
  const [slugStatus, setSlugStatus] = useState<'idle' | 'checking' | 'available' | 'taken' | 'invalid'>('idle');
  const [description, setDescription] = useState(list?.description ?? '');
  const [visibility, setVisibility] = useState<'private' | 'public_read' | 'public_edit'>(
    list?.visibility ?? 'private'
  );
  const [icon, setIcon] = useState<string | null>(list?.icon ?? null);
  const [color, setColor] = useState<string | null>(list?.color ?? null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const slugCheckTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auto-suggest slug from name when user hasn't manually edited it
  useEffect(() => {
    if (slugTouched || mode === 'edit') return;
    const fragment = nameToSlugFragment(name);
    if (fragment) {
      setSlug(`${username}/${fragment}`);
    } else {
      setSlug('');
    }
  }, [name, username, slugTouched, mode]);

  // Initialize slug auto-suggest for initialName on mount
  useEffect(() => {
    if (mode === 'create' && initialName && !slugTouched) {
      const fragment = nameToSlugFragment(initialName);
      if (fragment) {
        setSlug(`${username}/${fragment}`);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced slug availability check
  const checkAvailability = useCallback((value: string) => {
    if (slugCheckTimer.current) clearTimeout(slugCheckTimer.current);

    if (!value || value.length < 2) {
      setSlugStatus('idle');
      return;
    }

    // Basic client-side format validation
    if (!/^[a-z0-9]+(?:[-/][a-z0-9]+)*$/.test(value) || (value.match(/\//g) || []).length > 1) {
      setSlugStatus('invalid');
      return;
    }

    // Skip check if unchanged in edit mode
    if (mode === 'edit' && value === list?.slug) {
      setSlugStatus('available');
      return;
    }

    setSlugStatus('checking');
    slugCheckTimer.current = setTimeout(async () => {
      const { available, error: err } = await checkSlugAvailability(value, mode === 'edit' ? list?.id : undefined);
      if (err) {
        setSlugStatus('invalid');
      } else {
        setSlugStatus(available ? 'available' : 'taken');
      }
    }, 400);
  }, [mode, list?.id, list?.slug]);

  // Run availability check whenever slug changes
  useEffect(() => {
    checkAvailability(slug);
    return () => {
      if (slugCheckTimer.current) clearTimeout(slugCheckTimer.current);
    };
  }, [slug, checkAvailability]);

  const handleSlugChange = (value: string) => {
    // Normalize: lowercase, strip disallowed characters
    const normalized = value.toLowerCase().replace(/[^a-z0-9/-]/g, '');
    setSlug(normalized);
    setSlugTouched(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !slug || slugStatus === 'taken' || slugStatus === 'invalid') return;

    setSaving(true);
    setError(null);

    const result = mode === 'create'
      ? await createList(name.trim(), description.trim() || undefined, visibility, icon, color, slug)
      : await updateList(list!.id, {
          name: name.trim(),
          slug: slug !== list!.slug ? slug : undefined,
          description: description.trim() || undefined,
          visibility,
          icon,
          color,
        });

    setSaving(false);

    if (result.error) {
      setError(result.error);
      return;
    }

    if (mode === 'create' && onCreated && 'list' in result && result.list) {
      onCreated(result.list as ObjectList);
    }
    onSuccess();
  };

  const slugIcon = {
    idle: null,
    checking: <Loader2 className="w-3.5 h-3.5 animate-spin text-text-secondary dark:text-slate-500" />,
    available: <Check className="w-3.5 h-3.5 text-green-600 dark:text-green-400" />,
    taken: <XIcon className="w-3.5 h-3.5 text-red-500 dark:text-red-400" />,
    invalid: <XIcon className="w-3.5 h-3.5 text-red-500 dark:text-red-400" />,
  }[slugStatus];

  return (
    <form onSubmit={handleSubmit} className="border border-border dark:border-slate-700 rounded-lg p-4 bg-card dark:bg-slate-800/50">
      <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 mb-3">
        {mode === 'create' ? 'Create New Tag' : 'Edit Tag'}
      </h3>

      {error && (
        <div className="mb-3 p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded text-sm text-red-800 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="space-y-3">
        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Name
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., AGN Candidates"
            maxLength={100}
            minLength={2}
            className="w-full px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
            autoFocus
            required
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Shortname
          </label>
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-text-secondary dark:text-slate-500">#</span>
            <input
              type="text"
              value={slug}
              onChange={(e) => handleSlugChange(e.target.value)}
              placeholder={username ? `${username}/my-tag` : 'my-tag'}
              maxLength={60}
              className={`w-full pl-7 pr-8 py-2 text-sm font-mono border rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:border-transparent ${
                slugStatus === 'taken' || slugStatus === 'invalid'
                  ? 'border-red-300 dark:border-red-700 focus:ring-red-500'
                  : slugStatus === 'available'
                  ? 'border-green-300 dark:border-green-700 focus:ring-green-500'
                  : 'border-border dark:border-slate-600 focus:ring-primary'
              }`}
            />
            {slugIcon && (
              <span className="absolute right-3 top-1/2 -translate-y-1/2">
                {slugIcon}
              </span>
            )}
          </div>
          <p className="mt-1 text-[11px] text-text-secondary dark:text-slate-500">
            {slugStatus === 'taken'
              ? 'This shortname is already taken'
              : slugStatus === 'invalid'
              ? 'Lowercase letters, numbers, hyphens, and one optional slash'
              : 'Used as a hashtag identifier, e.g. #akins26-lrds'}
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Description
          </label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional description"
            rows={2}
            className="w-full px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent resize-none"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Icon
          </label>
          <ListEmojiPicker value={icon} onChange={setIcon} />
        </div>

        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Color
          </label>
          <ListColorPicker value={color} onChange={setColor} />
        </div>

        <div>
          <label className="block text-xs font-medium text-text-secondary dark:text-slate-400 mb-1">
            Visibility
          </label>
          <select
            value={visibility}
            onChange={(e) => setVisibility(e.target.value as typeof visibility)}
            className="w-full px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          >
            <option value="private">Private — only you can see</option>
            <option value="public_read">Public — others can view</option>
            <option value="public_edit">Collaborative — others can add/remove objects</option>
          </select>
        </div>
      </div>

      <div className="flex gap-2 mt-4">
        <Button type="button" variant="secondary" size="sm" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          size="sm"
          disabled={saving || name.trim().length < 2 || !slug || slugStatus === 'taken' || slugStatus === 'invalid' || slugStatus === 'checking'}
        >
          {saving ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin mr-1" />
              {mode === 'create' ? 'Creating...' : 'Saving...'}
            </>
          ) : (
            mode === 'create' ? 'Create Tag' : 'Save Changes'
          )}
        </Button>
      </div>
    </form>
  );
}
