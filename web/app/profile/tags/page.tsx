'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { ListBadge } from '@/components/lists/ListBadge';
import { ListForm } from '@/components/lists/ListForm';
import { useMyListsQuery } from '@/lib/hooks/useListsQuery';
import { deleteList } from '@/lib/actions/lists';
import { useAuth } from '@/lib/contexts/AuthContext';
import type { ObjectListOverview } from '@/lib/types';
import {
  LogIn,
  Loader2,
  Tag,
  Plus,
  Edit2,
  Trash2,
  ExternalLink,
  Hash,
  Calendar,
} from 'lucide-react';

export default function MyListsPage() {
  const router = useRouter();
  const { user, userProfile, loading: authLoading } = useAuth();
  const canComment = !!userProfile?.can_comment;

  const { data, isLoading, refetch } = useMyListsQuery(!authLoading && !!user);
  const lists = data?.lists ?? [];
  const error = data?.error ?? null;

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [editingListId, setEditingListId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const handleDelete = async (list: ObjectListOverview) => {
    if (!confirm(`Delete "${list.name}"? This will remove all memberships. This cannot be undone.`)) {
      return;
    }
    setDeletingId(list.id);
    const result = await deleteList(list.id);
    if (result.error) {
      alert(result.error);
    } else {
      refetch();
    }
    setDeletingId(null);
  };

  const breadcrumbs = [
    { label: 'CAMPFIRE', href: '/' },
    { label: 'Profile', href: '/profile' },
    { label: 'My Tags' },
  ];

  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs items={breadcrumbs} className="mb-6" />
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to manage your tags
          </h2>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors mt-4"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  if (isLoading || authLoading) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs items={breadcrumbs} className="mb-6" />
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading tags...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs items={breadcrumbs} className="mb-6" />

      <div className="max-w-3xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Tag className="w-8 h-8 text-primary" />
            <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">My Tags</h1>
          </div>
          {canComment && !showCreateForm && (
            <Button variant="primary" size="sm" onClick={() => setShowCreateForm(true)}>
              <Plus className="w-4 h-4 mr-1" />
              New Tag
            </Button>
          )}
        </div>

        {!canComment && (
          <div className="bg-amber-50 dark:bg-amber-950 border border-amber-200 dark:border-amber-800 rounded-lg p-4 text-sm text-amber-700 dark:text-amber-300">
            You need comment permissions to create and manage tags. Contact an admin for access.
          </div>
        )}

        {error && (
          <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
            <p className="text-red-800 dark:text-red-400">{error}</p>
          </div>
        )}

        {/* Create Form */}
        {showCreateForm && (
          <ListForm
            mode="create"
            onSuccess={() => {
              setShowCreateForm(false);
              refetch();
            }}
            onCancel={() => setShowCreateForm(false)}
          />
        )}

        {/* Lists */}
        {lists.length === 0 && !showCreateForm ? (
          <Card className="p-8 text-center">
            <Tag className="w-12 h-12 text-text-secondary dark:text-slate-500 mx-auto mb-4" />
            <p className="text-text-secondary dark:text-slate-400 mb-2">
              No tags yet.
            </p>
            {canComment && (
              <p className="text-sm text-text-secondary dark:text-slate-500">
                Create a tag to organize objects for your research.
              </p>
            )}
          </Card>
        ) : (
          <Card className="divide-y divide-border dark:divide-slate-700">
            {lists.map(list => (
              <div key={list.id} className="p-4">
                {editingListId === list.id ? (
                  <ListForm
                    mode="edit"
                    list={list}
                    onSuccess={() => {
                      setEditingListId(null);
                      refetch();
                    }}
                    onCancel={() => setEditingListId(null)}
                  />
                ) : (
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <Link
                          href={`/nirspec/tags/${list.slug}`}
                          className="text-base font-semibold text-text-primary dark:text-slate-100 hover:text-primary transition-colors truncate"
                        >
                          {list.name}
                        </Link>
                        <span className="text-xs font-mono text-text-secondary dark:text-slate-500">#{list.slug}</span>
                        <ListBadge visibility={list.visibility} />
                      </div>
                      {list.description && (
                        <p className="text-sm text-text-secondary dark:text-slate-400 line-clamp-1 mb-1">
                          {list.description}
                        </p>
                      )}
                      <div className="flex items-center gap-3 text-xs text-text-secondary dark:text-slate-500">
                        <span className="inline-flex items-center gap-1">
                          <Hash className="w-3 h-3" />
                          {list.member_count} {list.member_count === 1 ? 'object' : 'objects'}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Calendar className="w-3 h-3" />
                          {new Date(list.created_at).toLocaleDateString()}
                        </span>
                      </div>
                    </div>

                    <div className="flex items-center gap-1 flex-shrink-0">
                      <Link
                        href={`/nirspec/tags/${list.slug}`}
                        className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
                        title="View tag"
                      >
                        <ExternalLink className="w-4 h-4" />
                      </Link>
                      {canComment && (
                        <>
                          <button
                            onClick={() => setEditingListId(list.id)}
                            className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-700 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
                            title="Edit tag"
                          >
                            <Edit2 className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => handleDelete(list)}
                            disabled={deletingId === list.id}
                            className="p-2 rounded-lg text-text-secondary dark:text-slate-400 hover:bg-red-50 dark:hover:bg-red-950 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                            title="Delete tag"
                          >
                            {deletingId === list.id ? (
                              <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                              <Trash2 className="w-4 h-4" />
                            )}
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </Card>
        )}

        {/* Browse all lists link */}
        <div className="text-center">
          <Link
            href="/nirspec/tags"
            className="text-sm text-text-secondary dark:text-slate-400 hover:text-primary transition-colors"
          >
            Browse all public tags
          </Link>
        </div>
      </div>
    </div>
  );
}
