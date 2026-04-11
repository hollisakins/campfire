'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { ListBadge } from '@/components/lists/ListBadge';
import { ListForm } from '@/components/lists/ListForm';
import { ListMembersTable } from '@/components/lists/ListMembersTable';
import { useListDetailQuery } from '@/lib/hooks/useListsQuery';
import { deleteList } from '@/lib/actions/lists';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  LogIn,
  Loader2,
  Tag,
  ExternalLink,
  Edit2,
  Trash2,
  User,
  Calendar,
  Hash,
} from 'lucide-react';

export default function ListDetailPage() {
  const params = useParams();
  const router = useRouter();
  const slug = params.slug as string;
  const { user, loading: authLoading } = useAuth();

  const [page, setPage] = useState(1);
  const [isEditing, setIsEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const { data, isLoading, refetch } = useListDetailQuery(slug, page, !authLoading && !!user);
  const list = data?.list ?? null;
  const members = data?.members ?? [];
  const totalMembers = data?.totalMembers ?? 0;
  const error = data?.error ?? null;

  const isOwner = !!user && !!list && list.created_by === user.id;
  const canManage = isOwner && !list?.is_system;

  const handleDelete = async () => {
    if (!list || !confirm(`Delete "${list.name}"? This will remove all memberships. This cannot be undone.`)) {
      return;
    }
    setDeleting(true);
    const result = await deleteList(list.id);
    if (result.error) {
      alert(result.error);
      setDeleting(false);
    } else {
      router.push('/tags');
    }
  };

  const breadcrumbs = [
    { label: 'CAMPFIRE', href: '/' },
    { label: 'Tags', href: '/tags' },
    { label: list?.name ?? slug },
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
            Sign in to view this tag
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
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading tag...</span>
        </div>
      </div>
    );
  }

  if (error || !list) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs items={breadcrumbs} className="mb-6" />
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
          <p className="text-red-800 dark:text-red-400">{error || 'Tag not found'}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs items={breadcrumbs} className="mb-6" />

      <div className="max-w-5xl mx-auto space-y-6">
        {/* Header */}
        <Card className="p-6">
          {isEditing ? (
            <ListForm
              mode="edit"
              list={list}
              onSuccess={() => {
                setIsEditing(false);
                refetch();
              }}
              onCancel={() => setIsEditing(false)}
            />
          ) : (
            <>
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center gap-3">
                  {list.icon && <span className="text-2xl">{list.icon}</span>}
                  {list.color && !list.icon && (
                    <span
                      className="w-6 h-6 rounded-full flex-shrink-0"
                      style={{ backgroundColor: list.color }}
                    />
                  )}
                  <div>
                    <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">
                      {list.name}
                    </h1>
                    <div className="mt-1 flex items-center gap-2">
                      <span className="text-sm font-mono text-text-secondary dark:text-slate-400">#{list.slug}</span>
                      <ListBadge visibility={list.visibility} isSystem={list.is_system} size="md" />
                    </div>
                  </div>
                </div>

                {canManage && (
                  <div className="flex items-center gap-2">
                    <Button variant="secondary" size="sm" onClick={() => setIsEditing(true)}>
                      <Edit2 className="w-4 h-4 mr-1" />
                      Edit
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={handleDelete}
                      disabled={deleting}
                      className="text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950"
                    >
                      {deleting ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Trash2 className="w-4 h-4" />
                      )}
                    </Button>
                  </div>
                )}
              </div>

              {list.description && (
                <p className="text-text-secondary dark:text-slate-400 mb-4">
                  {list.description}
                </p>
              )}

              <div className="flex flex-wrap items-center gap-4 text-sm text-text-secondary dark:text-slate-400">
                <span className="inline-flex items-center gap-1.5">
                  <Hash className="w-4 h-4" />
                  {totalMembers.toLocaleString()} {totalMembers === 1 ? 'object' : 'objects'}
                </span>
                {list.creator_name && (
                  <span className="inline-flex items-center gap-1.5">
                    <User className="w-4 h-4" />
                    {list.creator_name}
                  </span>
                )}
                <span className="inline-flex items-center gap-1.5">
                  <Calendar className="w-4 h-4" />
                  Created {new Date(list.created_at).toLocaleDateString()}
                </span>
              </div>

              {/* Action buttons */}
              <div className="mt-4 pt-4 border-t border-border dark:border-slate-700">
                <Link
                  href={`/nirspec?view=objects&tags=${list.id}`}
                  className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
                >
                  <ExternalLink className="w-4 h-4" />
                  View in NIRSpec
                </Link>
              </div>
            </>
          )}
        </Card>

        {/* Members Table */}
        <div>
          <h2 className="text-lg font-semibold text-text-primary dark:text-slate-100 mb-4 flex items-center gap-2">
            <Tag className="w-5 h-5 text-primary" />
            Members
          </h2>
          <ListMembersTable
            members={members}
            totalMembers={totalMembers}
            page={page}
            pageSize={50}
            onPageChange={setPage}
          />
        </div>
      </div>
    </div>
  );
}
