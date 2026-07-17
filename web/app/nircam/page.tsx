'use client';

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { SignInLink } from '@/components/auth/SignInLink';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { NircamFieldCard } from '@/components/nircam/NircamFieldCard';
import { getNircamFields } from '@/lib/actions/nircam';
import type { NircamFieldCard as FieldCard } from '@/lib/types';
import { LogIn, Loader2, ImageIcon } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';

const formatVolume = (bytes: number): string => {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
};

export default function NircamLandingPage() {
  const { user, loading: authLoading } = useAuth();

  const [fields, setFields] = useState<FieldCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    if (authLoading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getNircamFields();
      if (result.error) {
        setError(result.error);
      } else {
        setFields(result.fields);
      }
    } catch (err) {
      setError('Failed to fetch fields');
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, [authLoading]);

  useEffect(() => {
    fetchData();
  }, [fetchData, user]);

  // Aggregate stat strip. Tiles and files are field-scoped, so sums are exact.
  const totals = useMemo(
    () => ({
      fields: fields.length,
      tiles: fields.reduce((s, f) => s + f.n_tiles, 0),
      files: fields.reduce((s, f) => s + f.n_files, 0),
      bytes: fields.reduce((s, f) => s + f.total_bytes, 0),
    }),
    [fields],
  );

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRCam' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view NIRCam images
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
            Access to NIRCam imaging data requires authentication. Please sign in with your
            CAMPFIRE account to browse and download images.
          </p>
          <SignInLink
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-on-primary rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </SignInLink>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRCam' },
        ]}
        className="mb-6"
      />

      <div className="mb-6">
        <div className="flex items-center gap-3">
          <ImageIcon className="w-8 h-8 text-primary" />
          <h1 className="text-2xl font-bold text-text-primary">NIRCam Imaging</h1>
        </div>
      </div>

      {/* Loading State */}
      {loading && (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
          <span className="ml-3 text-text-secondary">Loading fields...</span>
        </div>
      )}

      {/* Error State */}
      {error && !loading && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-4">
          <p className="text-red-800 dark:text-red-400">{error}</p>
        </div>
      )}

      {!loading && !error && (
        <>
          {fields.length === 0 ? (
            <div className="text-center py-16 bg-card border border-border rounded-lg">
              <ImageIcon className="w-12 h-12 text-text-secondary mx-auto mb-4" />
              <p className="text-text-secondary">No NIRCam fields available yet.</p>
              <p className="text-text-secondary text-sm mt-2">
                Check back later or contact the team if you expected to see data here.
              </p>
            </div>
          ) : (
            <>
              {/* Stat strip */}
              <div className="flex flex-wrap bg-card border border-border rounded-xl overflow-hidden mb-8">
                {[
                  [totals.fields, 'Fields'],
                  [totals.tiles, 'Tiles'],
                  [totals.files.toLocaleString(), 'Mosaic files'],
                  [formatVolume(totals.bytes), 'Total volume'],
                ].map(([n, l], i) => (
                  <div
                    key={l as string}
                    className={`flex-1 min-w-[130px] px-5 py-4 ${i > 0 ? 'border-l border-border' : ''}`}
                  >
                    <div className="text-xl font-bold text-text-primary">{n}</div>
                    <div className="text-[11px] uppercase tracking-wide text-text-tertiary mt-0.5">
                      {l}
                    </div>
                  </div>
                ))}
              </div>

              {/* Field cards */}
              <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(320px,1fr))]">
                {fields.map((card) => (
                  <NircamFieldCard key={card.field} card={card} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
