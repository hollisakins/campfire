'use client';

import React from 'react';
import Link from 'next/link';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { ListBadge } from '@/components/lists/ListBadge';
import { Card } from '@/components/ui/Card';
import { useListsOverviewQuery } from '@/lib/hooks/useListsQuery';
import { LogIn, Loader2, Tag, Hash, User, Shield } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

export default function ListsPage() {
  const { user, loading: authLoading } = useAuth();
  const { data, isLoading } = useListsOverviewQuery(!authLoading && !!user);
  const lists = data?.lists ?? [];
  const error = data?.error ?? null;

  const breadcrumbs = [
    { label: 'CAMPFIRE', href: '/' },
    { label: 'NIRSpec', href: '/nirspec' },
    { label: 'Tags' },
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
            Sign in to view tags
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Please sign in with your CAMPFIRE account to browse tags.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs items={breadcrumbs} className="mb-6" />

      {/* Page Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-2">
          <Tag className="w-8 h-8 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">Tags</h1>
        </div>
        <p className="text-text-secondary dark:text-slate-400">
          Tags let you classify unique objects in the NIRSpec database. Several system tags are provided by default,
          and you can create your own — privately or shared with collaborators. All tags are included in database
          downloads, so classifications carry through to your research workflows.
        </p>
      </div>

      {/* Content */}
      {isLoading || authLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading tags...</span>
        </div>
      ) : error ? (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      ) : lists.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <Tag className="w-12 h-12 text-text-secondary dark:text-slate-500 mb-4" />
          <p className="text-text-secondary dark:text-slate-400">No tags available yet.</p>
        </div>
      ) : (
        <Card className="overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="bg-card dark:bg-slate-800/50 border-b border-border dark:border-slate-700">
                <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-text-secondary dark:text-slate-400">Tag</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-text-secondary dark:text-slate-400 hidden md:table-cell">Description</th>
                <th className="px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-text-secondary dark:text-slate-400">Type</th>
                <th className="px-4 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-text-secondary dark:text-slate-400">Objects</th>
                <th className="px-4 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-text-secondary dark:text-slate-400 hidden sm:table-cell">Creator</th>
              </tr>
            </thead>
            <tbody>
              {lists.map(list => (
                <tr key={list.id} className="border-b border-border dark:border-slate-700/50 last:border-0 hover:bg-card-hover dark:hover:bg-slate-800/30 transition-colors">
                  <td className="px-4 py-3">
                    <Link href={`/nirspec/tags/${list.slug}`} className="flex items-center gap-2.5">
                      {list.icon ? (
                        <span className="text-lg flex-shrink-0">{list.icon}</span>
                      ) : list.color ? (
                        <span className="w-4 h-4 rounded-full flex-shrink-0" style={{ backgroundColor: list.color }} />
                      ) : (
                        <Tag className="w-4 h-4 text-text-secondary dark:text-slate-500 flex-shrink-0" />
                      )}
                      <div className="min-w-0">
                        <span className="font-medium text-text-primary dark:text-slate-100">{list.name}</span>
                        <span className="ml-2 text-xs font-mono text-text-secondary dark:text-slate-500">#{list.slug}</span>
                      </div>
                    </Link>
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell">
                    <span className="text-sm text-text-secondary dark:text-slate-400 line-clamp-1">{list.description || '—'}</span>
                  </td>
                  <td className="px-4 py-3">
                    <ListBadge visibility={list.visibility} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="inline-flex items-center gap-1 text-sm text-text-secondary dark:text-slate-400">
                      <Hash className="w-3 h-3" />
                      {list.member_count.toLocaleString()}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right hidden sm:table-cell">
                    {list.is_system ? (
                      <span className="inline-flex items-center gap-1 rounded-full font-medium text-[10px] px-1.5 py-0.5 bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300">
                        <Shield className="w-2.5 h-2.5" />
                        System
                      </span>
                    ) : list.creator_name ? (
                      <span className="inline-flex items-center gap-1 text-sm text-text-secondary dark:text-slate-400">
                        <User className="w-3 h-3" />
                        {list.creator_name}
                      </span>
                    ) : (
                      <span className="text-sm text-text-secondary dark:text-slate-500">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
