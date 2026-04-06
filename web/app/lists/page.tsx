'use client';

import React from 'react';
import Link from 'next/link';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { ListCard } from '@/components/lists/ListCard';
import { useListsOverviewQuery } from '@/lib/hooks/useListsQuery';
import { LogIn, Loader2, List } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

export default function ListsPage() {
  const { user, loading: authLoading } = useAuth();
  const { data, isLoading } = useListsOverviewQuery(!authLoading && !!user);
  const lists = data?.lists ?? [];
  const error = data?.error ?? null;

  const breadcrumbs = [
    { label: 'CAMPFIRE', href: '/' },
    { label: 'Lists' },
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
            Sign in to view lists
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Please sign in with your CAMPFIRE account to browse object lists.
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
          <List className="w-8 h-8 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100">Object Lists</h1>
        </div>
        <p className="text-text-secondary dark:text-slate-400 max-w-2xl">
          Curated collections of objects. System lists contain community classifications;
          user lists are custom selections for your research.
        </p>
      </div>

      {/* Content */}
      {isLoading || authLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary dark:text-slate-400">Loading lists...</span>
        </div>
      ) : error ? (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      ) : lists.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <List className="w-12 h-12 text-text-secondary dark:text-slate-500 mb-4" />
          <p className="text-text-secondary dark:text-slate-400">No lists available yet.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {lists.map(list => (
            <ListCard key={list.id} list={list} />
          ))}
        </div>
      )}
    </div>
  );
}
