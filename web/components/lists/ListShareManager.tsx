'use client';

import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Loader2, UserPlus, X as XIcon, Users } from 'lucide-react';
import {
  getListShares,
  searchUsersForSharing,
  addListShare,
  updateListShare,
  removeListShare,
} from '@/lib/actions/lists';
import type { ObjectListShareWithUser, ListShareRole } from '@/lib/types';

interface ListShareManagerProps {
  listId: number;
}

interface UserCandidate {
  user_id: string;
  username: string;
  full_name: string;
}

const ROLE_LABELS: Record<ListShareRole, string> = {
  viewer: 'Can view',
  editor: 'Can edit',
};

/**
 * Owner-facing management of per-user tag shares (issue #450): search users,
 * grant view/edit access, change roles, revoke. Rendered inside the tag edit
 * form (ListForm), which is only reachable by the tag owner.
 */
export function ListShareManager({ listId }: ListShareManagerProps) {
  const [shares, setShares] = useState<ObjectListShareWithUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState('');
  const [candidates, setCandidates] = useState<UserCandidate[]>([]);
  const [searching, setSearching] = useState(false);
  const [newRole, setNewRole] = useState<ListShareRole>('viewer');
  const [busy, setBusy] = useState(false);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const loadShares = useCallback(async () => {
    const result = await getListShares(listId);
    if (result.error) {
      setError(result.error);
    } else {
      setShares(result.shares);
    }
    setLoading(false);
  }, [listId]);

  useEffect(() => {
    loadShares();
  }, [loadShares]);

  // Debounced user search
  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setCandidates([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    searchTimer.current = setTimeout(async () => {
      const { users } = await searchUsersForSharing(trimmed);
      const sharedIds = new Set(shares.map(s => s.user_id));
      setCandidates(users.filter(u => !sharedIds.has(u.user_id)));
      setSearching(false);
    }, 300);
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
  }, [query, shares]);

  // Close the candidate dropdown on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setCandidates([]);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const handleAdd = async (candidate: UserCandidate) => {
    setBusy(true);
    setError(null);
    const result = await addListShare(listId, candidate.user_id, newRole);
    if (result.error) {
      setError(result.error);
    } else {
      setQuery('');
      setCandidates([]);
      await loadShares();
    }
    setBusy(false);
  };

  const handleRoleChange = async (share: ObjectListShareWithUser, role: ListShareRole) => {
    if (role === share.role) return;
    setBusy(true);
    setError(null);
    const result = await updateListShare(share.id, role);
    if (result.error) {
      setError(result.error);
    } else {
      setShares(prev => prev.map(s => (s.id === share.id ? { ...s, role } : s)));
    }
    setBusy(false);
  };

  const handleRemove = async (share: ObjectListShareWithUser) => {
    setBusy(true);
    setError(null);
    const result = await removeListShare(share.id);
    if (result.error) {
      setError(result.error);
    } else {
      setShares(prev => prev.filter(s => s.id !== share.id));
    }
    setBusy(false);
  };

  return (
    <div ref={containerRef}>
      <label className="block text-xs font-medium text-text-secondary mb-1">
        <span className="inline-flex items-center gap-1">
          <Users className="w-3.5 h-3.5" />
          Share with users
        </span>
      </label>
      <p className="mb-2 text-[11px] text-text-secondary dark:text-text-tertiary">
        Give specific users access to this tag regardless of its visibility. Editors can add and
        remove objects; viewers can only see the tag.
      </p>

      {error && (
        <div className="mb-2 p-2 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded text-xs text-red-800 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Add user */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by username or name..."
            disabled={busy}
            className="w-full px-3 py-2 text-sm border border-border-strong rounded-md bg-background text-text-primary placeholder:text-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          />
          {searching && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2">
              <Loader2 className="w-3.5 h-3.5 animate-spin text-text-secondary dark:text-text-tertiary" />
            </span>
          )}
          {candidates.length > 0 && (
            <div className="absolute z-10 mt-1 w-full border border-border rounded-md bg-card shadow-lg overflow-hidden">
              {candidates.map(candidate => (
                <button
                  key={candidate.user_id}
                  type="button"
                  onClick={() => handleAdd(candidate)}
                  disabled={busy}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-card-hover transition-colors"
                >
                  <UserPlus className="w-3.5 h-3.5 text-text-secondary flex-shrink-0" />
                  <span className="text-text-primary">{candidate.full_name}</span>
                  <span className="text-xs font-mono text-text-secondary">@{candidate.username}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <select
          value={newRole}
          onChange={(e) => setNewRole(e.target.value as ListShareRole)}
          disabled={busy}
          className="px-2 py-2 text-sm border border-border-strong rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
        >
          <option value="viewer">{ROLE_LABELS.viewer}</option>
          <option value="editor">{ROLE_LABELS.editor}</option>
        </select>
      </div>

      {/* Current shares */}
      {loading ? (
        <div className="flex items-center gap-2 py-3 text-xs text-text-secondary">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          Loading shares...
        </div>
      ) : shares.length > 0 ? (
        <ul className="mt-2 divide-y divide-border border border-border rounded-md">
          {shares.map(share => (
            <li key={share.id} className="flex items-center gap-2 px-3 py-2">
              <div className="flex-1 min-w-0 flex items-baseline gap-2">
                <span className="text-sm text-text-primary truncate">
                  {share.full_name ?? 'Unknown user'}
                </span>
                {share.username && (
                  <span className="text-xs font-mono text-text-secondary flex-shrink-0">
                    @{share.username}
                  </span>
                )}
              </div>
              <select
                value={share.role}
                onChange={(e) => handleRoleChange(share, e.target.value as ListShareRole)}
                disabled={busy}
                className="px-1.5 py-1 text-xs border border-border-strong rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
              >
                <option value="viewer">{ROLE_LABELS.viewer}</option>
                <option value="editor">{ROLE_LABELS.editor}</option>
              </select>
              <button
                type="button"
                onClick={() => handleRemove(share)}
                disabled={busy}
                className="p-1 rounded text-text-secondary hover:bg-red-50 dark:hover:bg-red-950 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                title="Revoke access"
              >
                <XIcon className="w-3.5 h-3.5" />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-text-secondary dark:text-text-tertiary">
          Not shared with anyone yet.
        </p>
      )}
    </div>
  );
}
