'use client';

import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { createList, updateList } from '@/lib/actions/lists';
import type { ObjectList } from '@/lib/types';

interface ListFormProps {
  mode: 'create' | 'edit';
  list?: ObjectList;
  onSuccess: () => void;
  onCancel: () => void;
}

export function ListForm({ mode, list, onSuccess, onCancel }: ListFormProps) {
  const [name, setName] = useState(list?.name ?? '');
  const [description, setDescription] = useState(list?.description ?? '');
  const [visibility, setVisibility] = useState<'private' | 'public_read' | 'public_edit'>(
    list?.visibility ?? 'private'
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setSaving(true);
    setError(null);

    const result = mode === 'create'
      ? await createList(name.trim(), description.trim() || undefined, visibility)
      : await updateList(list!.id, {
          name: name.trim(),
          description: description.trim() || undefined,
          visibility,
        });

    setSaving(false);

    if (result.error) {
      setError(result.error);
      return;
    }

    onSuccess();
  };

  return (
    <form onSubmit={handleSubmit} className="border border-border dark:border-slate-700 rounded-lg p-4 bg-card dark:bg-slate-800/50">
      <h3 className="text-sm font-semibold text-text-primary dark:text-slate-100 mb-3">
        {mode === 'create' ? 'Create New List' : 'Edit List'}
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
            className="w-full px-3 py-2 text-sm border border-border dark:border-slate-600 rounded-md bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
            autoFocus
            required
          />
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
        <Button type="submit" variant="primary" size="sm" disabled={saving || !name.trim()}>
          {saving ? (
            <>
              <Loader2 className="w-4 h-4 animate-spin mr-1" />
              {mode === 'create' ? 'Creating...' : 'Saving...'}
            </>
          ) : (
            mode === 'create' ? 'Create List' : 'Save Changes'
          )}
        </Button>
      </div>
    </form>
  );
}
